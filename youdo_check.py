#!/usr/bin/env python3
"""
Youdo.com order monitor with Telegram notifications.

Uses replay of captured mobile API signatures (KeyVersion=1).
The signature is deterministic — same URL → same signature, no timestamp/nonce.
This means a captured signature works indefinitely for the same URL.

API: GET https://api.youdo.com/v110/tasks/tasks?...
Auth: Signature header (HMAC-SHA256, key hardcoded in iOS app, KeyVersion=1)
Response: JSON with ResultObject.Items[] (base64-encoded when Accept-Encoding: br)

No LLM required — pure deterministic pipeline.
"""
import json
import logging
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

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
logger = logging.getLogger("youdo_check")

# Telegram: @fl_aibot (Freelance Jobs bot)
TG_BOT_TOKEN = "8776532572:AAGh2OnHOaUjZAs-M-04nluayq2-qM4O8fk"
TG_CHAT_ID = "128204572"

# Data directory
DATA_DIR = Path(os.path.expanduser("~/.hermes/data/youdo"))
if not (DATA_DIR.parent.parent / "scripts" / "youdo_check.py").exists():
    DATA_DIR = Path("/home/hermes/.hermes/data/youdo")
DATA_DIR.mkdir(parents=True, exist_ok=True)

SEEN_IDS_PATH = DATA_DIR / "youdo_seen_ids.json"
OUT_JSONL = DATA_DIR / "youdo_orders.jsonl"
ERROR_ALERT_PATH = DATA_DIR / "youdo_error_alerts.json"

# Youdo API: captured replay signature (KeyVersion=1)
# Category=4194304 = IT/Software Development ("Разработка ПО")
# This URL+signature combo works indefinitely for live task data.
YOUDO_URL = (
    "https://api.youdo.com/v110/tasks/tasks"
    "?status=Opened"
    "&onlySbr=false"
    "&onlyVirtual=false"
    "&noOffers=false"
    "&onlyB2b=false"
    "&onlyRecommended=false"
    "&onlyVacancies=false"
    "&Category=4194304"  # IT / Software Development
    "&lat=55.753215"
    "&lng=37.622504"
    "&radius=50.0"
    "&page=1"
    "&pageSize=50"
    "&SearchRequestId=8463F248-AD83-4538-8FFD-20FF6FAC7C90"
)

YOUDO_SIG = "UnqICVwA2OHRzcMAwyBCiEdoVizRqOf7Xar8l4Q6zyM="

YOUDO_HEADERS = {
    "Host": "api.youdo.com",
    "Accept": "*/*",
    "KeyVersion": "1",
    "X-FeatureSetId": "976",
    "Accept-Language": "ru",
    "Signature": YOUDO_SIG,
    "X-VisitorId": "E38D2FBB-515B-4B6D-AD76-665B0027C70A",
    "X-DeviceId": "E38D2FBB-515B-4B6D-AD76-665B0027C70A",
    "User-Agent": (
        "26.5.2,4.225.0.2994664,iosPhoneApp,430x932,3,apple,"
        "iPhone16_2,E38D2FBB-515B-4B6D-AD76-665B0027C70A"
    ),
    "Connection": "keep-alive",
    "msid": "6ccec869-4690-46b4-ad77-dcd41861f9cd",
}

# AI keyword filter — two groups:
# LONG keywords are unique enough to match anywhere (substring OK)
# SHORT keywords need word boundaries to avoid false positives
#   ("бот" in "обработать", "ai" in "again", "ml" in "html", etc.)
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
# Word boundary: \b doesn't work well with Cyrillic, so use lookarounds
# Match if surrounded by non-letter chars or string start/end
AI_RE_SHORT = re.compile(
    r"(?i)(?<![a-zа-яё])(?:"
    + "|".join(re.escape(k) for k in AI_KEYWORDS_SHORT)
    + r")(?![a-zа-яё])"
)


def is_ai_match(text: str) -> bool:
    """Check if text contains any AI keyword (with proper word boundaries for short ones)."""
    return bool(AI_RE_LONG.search(text) or AI_RE_SHORT.search(text))


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


def send_telegram(message: str, reply_markup: dict | None = None) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("Telegram credentials not set")
        return False
    try:
        url = f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage"
        # Telegram message limit is 4096 chars
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
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code != 200:
            logger.error(f"Telegram API error {resp.status_code}: {resp.text[:200]}")
            return False
        return True
    except Exception as e:
        logger.exception("Telegram send failed")
        return False


def cover_letter_keyboard(order_id: str) -> dict:
    """Inline keyboard with 'Написать отклик' button."""
    return {
        "inline_keyboard": [[
            {"text": "📝 Написать отклик", "callback_data": f"reply:youdo:{order_id}"}
        ]]
    }


def fetch_tasks() -> list:
    """Fetch tasks from Youdo API. Returns list of task dicts."""
    try:
        resp = requests.get(YOUDO_URL, headers=YOUDO_HEADERS, timeout=15)
        if resp.status_code != 200:
            logger.error(f"HTTP {resp.status_code}")
            return []

        data = resp.json()
        if not data.get("IsSuccess", True) and data.get("Code"):
            logger.error(f"API error: Code={data['Code']}, Message={data.get('Message', '')}")
            return []

        items = data.get("ResultObject", {}).get("Items", [])
        logger.info(f"Fetched {len(items)} tasks (total: {data.get('ResultObject',{}).get('TotalCount','?')})")
        return items
    except Exception as e:
        logger.error(f"Fetch failed: {e}")
        return []


def format_task_message(task: dict) -> str:
    """Format a task as a Telegram message."""
    task_id = task.get("Id", "?")
    name = task.get("Name", "Без названия")
    desc = task.get("Description", "")
    category = task.get("CategoryName", "")
    budget = task.get("Budget", {})
    max_price = budget.get("Max")
    min_price = budget.get("Min")
    location = task.get("Location", {}).get("Address", "")
    creator = task.get("CreatorName", "")
    date_publish = task.get("DatePublish")

    # Build price string
    if max_price and min_price:
        price = f"{min_price:.0f}–{max_price:.0f} ₽"
    elif max_price:
        price = f"до {max_price:.0f} ₽"
    elif min_price:
        price = f"от {min_price:.0f} ₽"
    else:
        price = "договорная"

    # Format date
    date_str = ""
    if date_publish:
        dt = datetime.fromtimestamp(date_publish / 1000, tz=timezone.utc)
        date_str = dt.strftime("%d.%m %H:%M")

    # Truncate description
    if desc and len(desc) > 300:
        desc = desc[:300] + "…"

    msg = f"#YouDo\n"
    msg += f"<b>{name}</b>\n"
    if desc:
        msg += f"\n{desc}\n"
    msg += f"\n💰 {price}\n"
    if date_str:
        msg += f"🕐 {date_str}\n"
    if location:
        msg += f"📍 {location}\n"
    if creator:
        msg += f"👤 {creator}\n"
    msg += f"\n🔗 https://youdo.com/t{task_id}"

    return msg


def main():
    logger.info("Starting Youdo.com order check")

    seen_ids = load_seen_ids()
    logger.info(f"Loaded {len(seen_ids)} seen ids")

    tasks = fetch_tasks()
    if not tasks:
        # If fetch fails, send error alert (rate-limited)
        if not was_error_alerted_recently("fetch_failed"):
            send_telegram("⚠️ [YouDo] Не удалось получить заказы (API недоступен)")
            mark_error_alerted("fetch_failed")
        return

    # Filter AI orders
    new_ai_orders = []
    for task in tasks:
        task_id = str(task.get("Id", ""))
        if task_id in seen_ids:
            continue

        text = (task.get("Name", "") + " " + task.get("Description", "")).lower()
        if is_ai_match(text):
            new_ai_orders.append(task)

    if not new_ai_orders:
        logger.info("No new AI orders from Youdo.com")
        # Clear error alert state if fetch succeeded
        alerts = load_error_alerts()
        alerts.pop("fetch_failed", None)
        save_error_alerts(alerts)
        return

    logger.info(f"Found {len(new_ai_orders)} new AI orders from Youdo.com")

    for task in new_ai_orders:
        msg = format_task_message(task)
        oid = str(task.get("Id", ""))
        if send_telegram(msg, reply_markup=cover_letter_keyboard(oid)):
            logger.info(f"Sent: [{task['Id']}] {task['Name'][:50]}")
        else:
            logger.warning(f"Failed to send: [{task['Id']}]")

        # Save to JSONL
        append_jsonl({
            "id": str(task.get("Id", "")),
            "title": task.get("Name", ""),
            "description": task.get("Description", ""),
            "price": task.get("Budget", {}).get("Max", ""),
            "location": task.get("Location", {}).get("Address", ""),
            "client": task.get("CreatorName", ""),
            "category": task.get("CategoryName", ""),
            "source": "youdo.com",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })

        # Mark as seen
        task_id = str(task.get("Id", ""))
        if task_id not in seen_ids:
            seen_ids.append(task_id)

    # Keep seen_ids list from growing unbounded (max 1000)
    if len(seen_ids) > 1000:
        seen_ids = seen_ids[-1000:]

    save_seen_ids(seen_ids)
    logger.info(f"Done. Seen ids: {len(seen_ids)}")


if __name__ == "__main__":
    main()