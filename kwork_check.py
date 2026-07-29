#!/usr/bin/env python3
"""
Kwork.ru AI order monitor with Telegram notifications.

Fetches projects from Kwork.ru via AJAX POST API (category 41 = IT/Software Development),
filters by AI keywords, deduplicates by project ID, sends matches to Telegram.
Writes to Google Sheets (freelance tracker spreadsheet, 'kwork' tab).

No LLM required — pure deterministic pipeline.
Designed for cron every 5 minutes.
"""
import json
import logging
import os
import re
import sys
import time
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import requests

try:
    from dotenv import load_dotenv
    _env_path = os.path.expanduser("~/.hermes/.env")
    if not os.path.exists(_env_path):
        _env_path = "/home/hermes/.hermes/.env"
    load_dotenv(_env_path)
except ImportError:
    pass

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger("kwork_check")

# Telegram: @fl_aibot (Freelance Jobs bot)
TG_BOT_TOKEN="8776532572:AAGh2OnHOaUjZAs-M-04nluayq2-qM4O8fk"
TG_CHAT_ID = "128204572"

# Data directory
DATA_DIR = Path(os.path.expanduser("~/.hermes/data/kwork"))
if not (DATA_DIR.parent.parent / "scripts" / "kwork_check.py").exists():
    DATA_DIR = Path("/home/hermes/.hermes/data/kwork")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SEEN_IDS_PATH = DATA_DIR / "kwork_seen_ids.json"
OUT_JSONL = DATA_DIR / "kwork_orders.jsonl"
ERROR_ALERT_PATH = DATA_DIR / "kwork_error_alerts.json"

# Kwork API
KWORK_URL = "https://kwork.ru/projects"
KWORK_HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/148.0.0.0 Safari/537.36",
    "Accept": "application/json",
    "Accept-Language": "ru-RU,ru;q=0.9",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
    "Referer": "https://kwork.ru/projects?c=41",
}
# Category 41 = IT / Software Development
KWORK_CATEGORY = 41
KWORK_PAGE_SIZE = 12  # API default

# Google Sheets — freelance tracker
SPREADSHEET_ID = "1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok"
# GID will be resolved by sheet name 'kwork' at runtime

# AI keyword filter — same as Profi/Youdo
AI_KEYWORDS_LONG = [
    "искусственный интеллект", "нейросет", "нейронная сеть",
    "artificial intelligence", "machine learning",
    "chatgpt", "openai", "data science", "data scientist",
    "prompt engineering", "automation",
    "n8n", "make.com", "no-code", "low-code",
    "машинное обучение", "deep learning", "обучение нейросет",
    "генеративный", "generative ai", "large language model",
    "предиктивная аналитика", "распознавание", "computer vision",
    "анализ данных", "data analysis",
    "natural language processing",
    "fine-tuning", "finetune", "дообучение", "embedding", "векторная база",
    "retrieval augmented generation",
    "autogpt", "мультиагент", "multi-agent", "api интеграция",
]
AI_KEYWORDS_SHORT = [
    "ии", "ai", "ml", "бот", "бота", "боту", "bot", "agent", "агент",
    "rag", "llm", "ocr", "nlp", "prompt", "промт",
    "copilot", "dify", "langchain",
]
AI_RE_LONG = re.compile(r"(?i)(" + "|".join(re.escape(k) for k in AI_KEYWORDS_LONG) + ")")
AI_RE_SHORT = re.compile(
    r"(?i)(?<![a-zа-яё])(?:"
    + "|".join(re.escape(k) for k in AI_KEYWORDS_SHORT)
    + r")(?![a-zа-яё])"
)


def is_ai_match(text: str) -> bool:
    """Check if text contains any AI keyword (with proper word boundaries for short ones)."""
    return bool(AI_RE_LONG.search(text) or AI_RE_SHORT.search(text))


# ── State / Dedup ─────────────────────────────────────────
def load_seen_ids() -> list:
    if not SEEN_IDS_PATH.exists():
        return []
    try:
        return json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []


def save_seen_ids(ids: list) -> None:
    SEEN_IDS_PATH.write_text(
        json.dumps(ids, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_jsonl(obj: dict) -> None:
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


# ── Error alerts ──────────────────────────────────────────
def load_error_alerts() -> dict:
    if not ERROR_ALERT_PATH.exists():
        return {}
    try:
        return json.loads(ERROR_ALERT_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_error_alerts(data: dict) -> None:
    ERROR_ALERT_PATH.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def was_error_alerted_recently(error_key: str, cooldown_seconds: int = 3600) -> bool:
    alerts = load_error_alerts()
    last = alerts.get(error_key, 0)
    return (time.time() - last) < cooldown_seconds


def mark_error_alerted(error_key: str) -> None:
    alerts = load_error_alerts()
    alerts[error_key] = int(time.time())
    save_error_alerts(alerts)


# ── Telegram ──────────────────────────────────────────────
def send_telegram(message: str, reply_markup: dict | None = None) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("Telegram credentials not set")
        return False
    try:
        if len(message) > 4000:
            message = message[:4000] + "…"
        payload = {
            "chat_id": TG_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=10,
        )
        if resp.status_code != 200:
            logger.error(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception:
        logger.exception("Telegram send failed")
        return False


def cover_letter_keyboard(order_id: str) -> dict:
    """Inline keyboard with 'Написать отклик' button."""
    return {
        "inline_keyboard": [[
            {"text": "📝 Написать отклик", "callback_data": f"reply:kwork:{order_id}"}
        ]]
    }


# ── Kwork API ─────────────────────────────────────────────
def fetch_projects(max_pages: int = 3) -> list:
    """Fetch projects from Kwork.ru AJAX API. Returns list of project dicts."""
    all_items = []
    for page in range(1, max_pages + 1):
        try:
            resp = requests.post(
                KWORK_URL,
                headers=KWORK_HEADERS,
                data={"c": KWORK_CATEGORY, "page": page},
                timeout=20,
            )
            if resp.status_code != 200:
                logger.error(f"HTTP {resp.status_code} on page {page}")
                break

            data = resp.json()
            if not data.get("success", False):
                logger.error(f"API returned success=false: {data.get('message', '')}")
                break

            items = data.get("data", {}).get("pagination", {}).get("data", [])
            if not items:
                break

            all_items.extend(items)
            logger.info(f"Page {page}: fetched {len(items)} projects")

            # Check if there are more pages
            last_page = data.get("data", {}).get("pagination", {}).get("last_page", 1)
            if page >= last_page:
                break

            time.sleep(1)  # Rate limit between pages
        except Exception as e:
            logger.error(f"Fetch failed on page {page}: {e}")
            break

    logger.info(f"Total fetched: {len(all_items)} projects")
    return all_items


def extract_price(project: dict) -> str:
    """Extract price string from project."""
    price_limit = project.get("priceLimit", "")
    possible = project.get("possiblePriceLimit", "")
    if price_limit and possible and str(possible) != str(price_limit):
        return f"{float(price_limit):.0f}–{float(possible):.0f} ₽"
    if price_limit:
        return f"до {float(price_limit):.0f} ₽"
    return "договорная"


def format_project_message(project: dict) -> str:
    """Format a project as a Telegram message — same style as Profi.ru."""
    pid = project.get("id", "?")
    name = project.get("name", "Без названия")
    desc = project.get("description", "")
    price = extract_price(project)
    username = project.get("user", {}).get("username", "")
    date_create = project.get("wantDates", {}).get("dateCreate", "")
    max_days = project.get("max_days", "")

    # Truncate description
    if desc and len(desc) > 300:
        desc = desc[:300] + "…"

    msg = "#Kwork\n"
    msg += f"<b>{name}</b>\n"
    if desc:
        msg += f"\n{desc}\n"
    msg += f"\n💰 {price}\n"
    if date_create:
        msg += f"🕐 {date_create}\n"
    if max_days:
        msg += f"⏱ Срок: {max_days} дн.\n"
    if username:
        msg += f"👤 {username}\n"
    msg += f"\n🔗 https://kwork.ru/projects/{pid}"

    return msg


# ── Google Sheets ─────────────────────────────────────────
def get_gws_access_token() -> str:
    creds_path = os.path.expanduser("~/.config/gws/credentials.json")
    if not os.path.exists(creds_path):
        raise FileNotFoundError("GWS credentials not found")
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


def find_sheet_gid(access_token: str, sheet_name: str = "kwork.ru") -> int | None:
    """Find sheet GID by name."""
    req = Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        info = json.load(resp)
    for sheet in info.get("sheets", []):
        if sheet["properties"]["title"].lower() == sheet_name.lower():
            return sheet["properties"]["sheetId"]
    return None


def create_sheet_tab(access_token: str, sheet_name: str = "kwork.ru") -> int:
    """Create a new sheet tab."""
    body = json.dumps({
        "requests": [{
            "addSheet": {
                "properties": {"title": sheet_name}
            }
        }]
    }).encode()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}:batchUpdate"
    req = Request(url, data=body, method="POST")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")
    with urlopen(req) as resp:
        result = json.load(resp)
    return result["replies"][0]["addSheet"]["properties"]["sheetId"]


def write_to_google_sheets(projects: list) -> int:
    """Write projects to Google Sheets 'kwork' tab."""
    if not projects:
        return 0

    try:
        access_token = get_gws_access_token()
    except Exception as e:
        logger.warning(f"Google Sheets auth failed: {e}")
        return 0

    # Find or create 'kwork' tab
    gid = find_sheet_gid(access_token, "kwork.ru")
    if gid is None:
        try:
            gid = create_sheet_tab(access_token, "kwork.ru")
            logger.info(f"Created 'kwork' tab (gid={gid})")
        except Exception as e:
            logger.warning(f"Failed to create 'kwork' tab: {e}")
            return 0

    # Find sheet name by gid (need exact title for range)
    req = Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        info = json.load(resp)
    sheet_name = None
    for sheet in info.get("sheets", []):
        if sheet["properties"]["sheetId"] == gid:
            sheet_name = sheet["properties"]["title"]
            break
    if not sheet_name:
        return 0

    sheet_encoded = urllib.parse.quote(str(sheet_name), safe="")

    # Get current row count
    req = Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_encoded}!A:A"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urlopen(req) as resp:
        values_data = json.load(resp)
    current_rows = len(values_data.get("values", []))
    next_row = current_rows + 1
    today = datetime.now().strftime("%d.%m.%Y")

    rows = []
    for p in projects:
        rows.append([
            today,                           # A: Date
            p.get("name", ""),               # B: Title
            extract_price(p),                # C: Price
            "Проект",                        # D: Type
            p.get("wantDates", {}).get("dateCreate", ""),  # E: Published
            p.get("description", "")[:500],  # F: Description
            f"https://kwork.ru/projects/{p.get('id', '')}",  # G: URL
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
        logger.info(f"Written {written} rows to '{sheet_name}'")
        return written
    except Exception as e:
        logger.error(f"Sheets write error: {e}")
        return 0


# ── Main ──────────────────────────────────────────────────
def main():
    logger.info("Starting Kwork.ru order check")

    seen_ids = load_seen_ids()
    logger.info(f"Loaded {len(seen_ids)} seen ids")

    # Fetch projects
    projects = fetch_projects(max_pages=3)
    if not projects:
        if not was_error_alerted_recently("fetch_failed"):
            send_telegram("⚠️ <b>[kwork.ru]</b>\n\nНе удалось получить проекты (API недоступен)")
            mark_error_alerted("fetch_failed")
        return

    # Filter new AI orders
    new_ai_orders = []
    for project in projects:
        pid = str(project.get("id", ""))
        if not pid or pid in seen_ids:
            continue

        text = (project.get("name", "") + " " + project.get("description", "")).lower()
        if is_ai_match(text):
            new_ai_orders.append(project)

    if not new_ai_orders:
        logger.info("No new AI orders from Kwork.ru")
        # Clear error alert state if fetch succeeded
        alerts = load_error_alerts()
        alerts.pop("fetch_failed", None)
        save_error_alerts(alerts)
        return

    logger.info(f"Found {len(new_ai_orders)} new AI orders from Kwork.ru")

    # Write to Google Sheets
    written = write_to_google_sheets(new_ai_orders)
    if written:
        logger.info(f"Google Sheets: {written} rows written")

    # Send to Telegram
    for project in new_ai_orders:
        msg = format_project_message(project)
        pid = str(project.get("id", ""))
        if send_telegram(msg, reply_markup=cover_letter_keyboard(pid)):
            logger.info(f"Sent: [{project['id']}] {project['name'][:50]}")
        else:
            logger.warning(f"Failed to send: [{project['id']}]")

        # Save to JSONL
        append_jsonl({
            "id": str(project.get("id", "")),
            "title": project.get("name", ""),
            "description": project.get("description", ""),
            "price": extract_price(project),
            "username": project.get("user", {}).get("username", ""),
            "category_id": project.get("category_id", ""),
            "source": "kwork.ru",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })

        # Mark as seen
        pid = str(project.get("id", ""))
        if pid and pid not in seen_ids:
            seen_ids.append(pid)

        time.sleep(0.5)  # Telegram rate limit

    # Keep seen_ids from growing unbounded (max 1000)
    if len(seen_ids) > 1000:
        seen_ids = seen_ids[-1000:]

    save_seen_ids(seen_ids)
    logger.info(f"Done. Seen ids: {len(seen_ids)}")


if __name__ == "__main__":
    main()