#!/usr/bin/env python3
"""
freelance.ru AI Tasks Scraper

Scrapes freelance.ru AI category, filters by keywords, deduplicates,
writes to Google Sheets, sends new jobs to Telegram bot @fl_aibot.

Usage: python3 freelance_ru_scraper.py
Output: Telegram messages + stderr debug log
"""
import json
import re
import os
import sys
import time
from datetime import datetime, timezone, timedelta

import urllib.parse
from urllib.request import Request, urlopen
from urllib.error import URLError

import requests
from bs4 import BeautifulSoup

# ── Config ──────────────────────────────────────────────
BASE_URL = "https://freelance.ru"
CATEGORY_URL = f"{BASE_URL}/task?c%5B%5D=724"  # AI category
STATE_FILE = os.path.expanduser("~/.hermes/freelance_ru_seen_urls.json")
SEEN_EXPIRE_HOURS = 720  # 30 days

HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml",
    "Accept-Language": "ru-RU,ru;q=0.9",
}

# Google Sheets
SPREADSHEET_ID = "1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok"
GID = 300837051  # freelance.ru tab
SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit#gid={GID}"

# ── Keywords ────────────────────────────────────────────
POSITIVE_KEYWORDS = [
    "ai агент", "ai-агент", "ии агент", "ии-агент",
    "rag", "retrieval augmented",
    "whisper", "транскрибаци", "транскрипци",
    "чатбот", "чат-бот", "chatbot", "чат бот",
    "ai бот", "ии бот", "ai-бот", "ии-бот",
    "мультиагент", "multi-agent", "multiagent",
    "langchain", "langgraph", "langflow",
    "n8n",
    "ai разработчик", "ии разработчик", "ai-разработчик",
    "ai-систем", "ии-систем",
    "ai-система", "ии-система",
    "claude code", "codex", "cursor",
    "агент закупок", "агент мониторинг",
    "llm", "large language model",
    "vector database", "векторная баз",
    "embedding", "эмбеддинг",
    "semantic search", "семантический поиск",
    "prompt engineering", "промпт инжиниринг",
    "fine-tuning", "файнтюнинг", "дообучение",
    "ai automation", "ии автоматизаци",
    "ai pipeline", "ai пайплайн", "ai-пайплайн",
    "голосовой робот", "voice bot", "voicebot",
    "ai-помощник", "ии-помощник",
    "copilot",
    # freelance.ru additions
    "workflow автоматизация",
    "автоматизация",
    "openai",
    "gpt",
    "агент",
    "разработка",
    "создание",
    "написание",
]

NEGATIVE_KEYWORDS = [
    "smm", "социальные сети", "соцсети", "social media",
    "3d график", "stl", "3d модел", "3d-модел",
    "модных луков", "луков одежды", "fashion",
    "разметка", "асессор", "полигональная разметка",
    "контент-завод", "контент завод",
    "ai-аватар", "ai аватар",
    "ai видео", "ai ролик",
    "карточка для рекламы",
    "ai-ассистент / smm",
    "ai-артист", "ии-артист",
    "отцифровать", "оцифровать",
    "комикс",
    "nsfw",
]

MONTHS = {
    "января": 1, "февраля": 2, "марта": 3, "апреля": 4,
    "мая": 5, "июня": 6, "июля": 7, "августа": 8,
    "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12,
}

# ── Telegram: @fl_aibot ──────────────────────────────────
TG_BOT_TOKEN="8776532572:AAGh2OnHOaUjZAs-M-04nluayq2-qM4O8fk"
TG_CHAT_ID = "128204572"


# ── Time parsing ─────────────────────────────────────────
def parse_posted_time(text, now=None):
    """Convert freelance.ru time string to datetime. Returns None if unparseable."""
    if now is None:
        now = datetime.now(timezone.utc)
    text = text.strip().lower()

    # "X часа Y минут назад", "X часов Y минуты назад"
    m = re.match(r"(\d+)\s+час\w*\s+(\d+)\s+минут\w*\s+назад", text)
    if m:
        return now - timedelta(hours=int(m.group(1)), minutes=int(m.group(2)))

    # "X минут назад"
    m = re.match(r"(\d+)\s+минут\w*\s+назад", text)
    if m:
        return now - timedelta(minutes=int(m.group(1)))

    # "X часов назад"
    m = re.match(r"(\d+)\s+час\w*\s+назад", text)
    if m:
        return now - timedelta(hours=int(m.group(1)))

    # "DD месяца, HH:MM"
    m = re.match(r"(\d+)\s+(\w+),\s*(\d+):(\d+)", text)
    if m:
        day, month_name, hour, minute = m.groups()
        month = MONTHS.get(month_name)
        if month:
            return datetime(now.year, month, int(day), int(hour), int(minute), tzinfo=timezone.utc)

    # "DD.MM.YYYY HH:MM" (from title attribute)
    m = re.match(r"(\d+)\.(\d+)\.(\d+)\s+(\d+):(\d+)", text)
    if m:
        day, month, year, hour, minute = m.groups()
        return datetime(int(year), int(month), int(day), int(hour), int(minute), tzinfo=timezone.utc)

    # "день назад" / "дня назад" / "дней назад"
    m = re.match(r"(\d+)\s+дн\w*\s+назад", text)
    if m:
        return now - timedelta(days=int(m.group(1)))
    if text == "день назад":
        return now - timedelta(days=1)

    return None


def is_within_24h(time_text, now=None):
    dt = parse_posted_time(time_text, now)
    if dt is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() <= 24 * 3600


# ── Relevance ────────────────────────────────────────────
def is_relevant(title, short_desc):
    text = f"{title} {short_desc}".lower()
    for kw in NEGATIVE_KEYWORDS:
        if kw in text:
            return False
    for kw in POSITIVE_KEYWORDS:
        if kw in text:
            return True
    return False


def compute_score(title, short_desc):
    text = f"{title} {short_desc}".lower()
    return sum(1 for kw in POSITIVE_KEYWORDS if kw in text)


# ── Scraping ─────────────────────────────────────────────
def fetch_full_description(task_url):
    """Fetch full task description from individual task page."""
    try:
        resp = requests.get(task_url, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[freelance.ru] Fetch desc error for {task_url}: {e}", file=sys.stderr)
        return ""

    soup = BeautifulSoup(resp.text, "html.parser")
    container = soup.select_one("main") or soup.select_one(".task-page")
    if not container:
        return ""

    text = container.get_text(separator="\n", strip=True)
    lines = text.split("\n")

    # Extract description: between h1 and "ГОНОРАР" or "Требуемые навыки"
    desc_lines = []
    in_desc = False
    for line in lines:
        low = line.lower()
        if "гоНАР" in line or "гонорар" in low or "требуемые навыки" in low:
            break
        if in_desc and line.strip():
            desc_lines.append(line.strip())
        # Start after h1 (first line is usually the title)
        h1 = container.find("h1")
        if h1 and line.strip() == h1.get_text(strip=True):
            in_desc = True

    # Fallback: if desc_lines empty, take all text up to stop markers
    if not desc_lines:
        h1 = container.find("h1")
        start_idx = 0
        if h1:
            h1_text = h1.get_text(strip=True)
            for i, line in enumerate(lines):
                if line.strip() == h1_text:
                    start_idx = i + 1
                    break
        for line in lines[start_idx:]:
            low = line.lower()
            if "гонорар" in low or "требуемые навыки" in low:
                break
            if line.strip():
                desc_lines.append(line.strip())

    return "\n".join(desc_lines)


def scrape_page(url):
    try:
        resp = requests.get(url, headers=HEADERS, timeout=30)
        resp.raise_for_status()
    except requests.RequestException as e:
        print(f"[freelance.ru] Fetch error: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(resp.text, "html.parser")
    projects = []
    for card in soup.select(".task-card"):
        title_el = card.select_one(".task-card__title-link")
        if not title_el:
            continue
        title = title_el.text.strip()
        href = title_el.get("href", "")
        link = BASE_URL + href if href.startswith("/") else href

        budget_el = card.select_one(".task-card__budget")
        budget = budget_el.text.strip() if budget_el else "—"

        desc_el = card.select_one(".task-card__desc")
        short_desc = desc_el.text.strip() if desc_el else ""

        # Time: element with title attribute
        time_el = card.select_one(".task-card__foot-item[title]")
        time_str = ""
        if time_el:
            time_str = time_el.get("title", "") or time_el.text.strip()

        # Job type is always "Задание" on freelance.ru
        project_type = "Задание"

        # Job ID from URL
        jid_match = re.search(r"/task/view/(\d+)", link)
        job_id = jid_match.group(1) if jid_match else link

        projects.append({
            "id": job_id,
            "title": title,
            "link": link,
            "budget": budget,
            "short_desc": short_desc,
            "type": project_type,
            "time": time_str,
            "score": compute_score(title, short_desc),
        })

    return projects


def scrape_all_pages(max_pages=5):
    all_projects = []
    for page in range(1, max_pages + 1):
        url = CATEGORY_URL if page == 1 else f"{CATEGORY_URL}&page={page}"
        print(f"[freelance.ru] Scraping page {page}: {url}", file=sys.stderr)
        projects = scrape_page(url)
        if not projects:
            break
        all_projects.extend(projects)
        # Stop if last project > 24h
        last_time = projects[-1]["time"]
        if not is_within_24h(last_time):
            break
    return all_projects


# ── State / Dedup ────────────────────────────────────────
def load_seen():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except Exception:
            pass
    return {}


def save_seen(seen):
    json.dump(seen, open(STATE_FILE, "w"), indent=2)


def dedup_and_filter(jobs, seen):
    now = datetime.now(timezone.utc)
    new_seen = dict(seen)

    # Expire old entries
    for jid, ts in list(new_seen.items()):
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
            if age > SEEN_EXPIRE_HOURS * 3600:
                del new_seen[jid]
        except Exception:
            del new_seen[jid]

    new_jobs = []
    skipped_old = 0
    for job in jobs:
        if not is_within_24h(job["time"], now):
            skipped_old += 1
            continue
        if job["id"] not in new_seen:
            new_jobs.append(job)
            new_seen[job["id"]] = now.isoformat()

    if skipped_old:
        print(f"[freelance.ru] Skipped {skipped_old} jobs older than 24h", file=sys.stderr)

    return new_jobs, new_seen


# ── Google Sheets ────────────────────────────────────────
def get_access_token():
    creds_path = os.path.expanduser("~/.config/gws/credentials.json")
    if not os.path.exists(creds_path):
        return None
    with open(creds_path) as fh:
        creds = json.load(fh)
    token_data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = Request("https://oauth2.googleapis.com/token", data=token_data)
    with urlopen(req) as resp:
        return json.load(resp)["access_token"]


def get_total_rows(access_token):
    req = Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        sheet_info = json.load(resp)

    sheet_name = None
    for sheet in sheet_info["sheets"]:
        if sheet["properties"]["sheetId"] == GID:
            sheet_name = sheet["properties"]["title"]
            break
    if sheet_name is None:
        return 0

    from urllib.parse import quote
    sheet_encoded = quote(str(sheet_name), safe="")
    req = Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_encoded}!A:A"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        values_data = json.load(resp)

    rows = values_data.get("values", [])
    total_rows = len(rows)
    if total_rows > 0 and rows[0] and rows[0][0].lower() == "date":
        total_rows -= 1
    return total_rows


def write_to_google_sheets(jobs):
    if not jobs:
        return 0

    creds_path = os.path.expanduser("~/.config/gws/credentials.json")
    if not os.path.exists(creds_path):
        print("[freelance.ru] No Google Sheets credentials, skipping write", file=sys.stderr)
        return 0

    with open(creds_path) as fh:
        creds = json.load(fh)

    token_data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = Request("https://oauth2.googleapis.com/token", data=token_data)
    with urlopen(req) as resp:
        access_token = json.load(resp)["access_token"]

    # Find sheet name by gid
    req = Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        sheet_info = json.load(resp)

    sheet_name = None
    for sheet in sheet_info["sheets"]:
        if sheet["properties"]["sheetId"] == GID:
            sheet_name = sheet["properties"]["title"]
            break

    if sheet_name is None:
        print(f"[freelance.ru] Sheet with gid {GID} not found", file=sys.stderr)
        return 0

    from urllib.parse import quote
    sheet_encoded = quote(str(sheet_name), safe="")

    req = Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_encoded}!A:E"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        values_data = json.load(resp)

    current_rows = len(values_data.get("values", []))
    next_row = current_rows + 1
    today = datetime.now().strftime("%d.%m.%Y")

    rows = []
    for j in jobs:
        rows.append([
            today,              # A: Date
            j["title"],         # B: Title
            j["budget"],        # C: Price
            j["type"],          # D: Type
            j["time"],          # E: Published
            j.get("description", ""),  # F: Description
            j["link"],          # G: URL
        ])

    end_row = next_row + len(rows) - 1
    range_str = f"{sheet_encoded}!A{next_row}:G{end_row}"
    body = json.dumps({"values": rows}).encode()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"

    req = Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urlopen(req) as resp:
            result = json.load(resp)
        written = result.get("updatedRows", 0)
        print(f"[freelance.ru] Written {written} rows to '{sheet_name}'", file=sys.stderr)
        return written
    except Exception as e:
        print(f"[freelance.ru] Sheets ERROR: {e}", file=sys.stderr)
        return 0


# ── Telegram ─────────────────────────────────────────────
def send_telegram(text, parse_mode="HTML", reply_markup=None):
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        return False
    try:
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        print(f"[freelance.ru] Telegram send failed: {e}", file=sys.stderr)
        return False


def cover_letter_keyboard(order_id: str) -> dict:
    """Inline keyboard with 'Написать отклик' button."""
    return {
        "inline_keyboard": [[
            {"text": "📝 Написать отклик", "callback_data": f"reply:freelance:{order_id}"}
        ]]
    }


def format_order(job):
    """Format a single job as HTML message for Telegram."""
    title = job["title"].rstrip(":").strip()
    budget = job.get("budget", "—")
    desc = job.get("description") or job.get("short_desc", "")
    if len(desc) > 300:
        desc = desc[:300] + "…"
    link = job["link"]
    job_type = job.get("type", "")
    time_str = job.get("time", "")

    lines = ["#Freelance", ""]
    lines.append(f"<b>{title}</b>")
    lines.append("")
    if budget and budget != "—":
        lines.append(f"💰 {budget}")
    if job_type:
        lines.append(f"📋 {job_type}")
    if time_str:
        lines.append(f"🕐 {time_str}")
    if desc:
        lines.append(f"📝 {desc}")
    lines.append("")
    lines.append(f'🔗 <a href="{link}">{job["id"]}</a>')

    return "\n".join(lines)


# ── Main ─────────────────────────────────────────────────
def main():
    print("[freelance.ru Scraper] Starting...", file=sys.stderr)

    # Scrape
    all_projects = scrape_all_pages(max_pages=5)
    print(f"[freelance.ru] Scraped {len(all_projects)} projects total", file=sys.stderr)

    # Filter relevant
    relevant = [p for p in all_projects if is_relevant(p["title"], p["short_desc"])]
    print(f"[freelance.ru] {len(relevant)} relevant after keyword filter", file=sys.stderr)

    # Sort by score (most relevant first)
    relevant.sort(key=lambda x: x["score"], reverse=True)

    # Dedup
    seen = load_seen()
    new_jobs, new_seen = dedup_and_filter(relevant, seen)
    print(f"[freelance.ru] {len(new_jobs)} new jobs after dedup", file=sys.stderr)

    # Fetch full descriptions for new jobs
    for job in new_jobs:
        print(f"[freelance.ru] Fetching full desc for job {job['id']}", file=sys.stderr)
        job["description"] = fetch_full_description(job["link"])

    # Write to Google Sheets
    if new_jobs:
        written = write_to_google_sheets(new_jobs)
        print(f"[freelance.ru] Sheets write result: {written} rows", file=sys.stderr)

    # Send to Telegram
    if new_jobs:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        header = f"🤖 <b>[freelance.ru] Новые AI-заказы: {len(new_jobs)}</b>\n<i>Время поиска: {now_str}</i>\n"
        print(f"[freelance.ru] Sending Telegram header...", file=sys.stderr)
        send_telegram(header)
        time.sleep(0.5)

        for job in new_jobs:
            msg = format_order(job)
            print(f"[freelance.ru] Sending job {job['id']} to Telegram...", file=sys.stderr)
            send_telegram(msg, reply_markup=cover_letter_keyboard(job["id"]))
            time.sleep(0.5)

    # Save seen
    save_seen(new_seen)
    print(f"[freelance.ru] Saved {len(new_seen)} seen IDs", file=sys.stderr)
    print("[freelance.ru Scraper] Done.", file=sys.stderr)


if __name__ == "__main__":
    main()