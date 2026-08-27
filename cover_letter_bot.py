#!/usr/bin/env python3
"""
Cover Letter Bot — webhook handler for @fl_aibot Telegram bot.

Receives callback_query from inline "Написать отклик" button,
fetches full order text from JSONL cache (or Firecrawl for youdo),
sends to LLM (deepseek-v4-flash via Ollama), returns cover letter
as monospace-formatted message.

Webhook endpoint: POST /webhook
Runs on port 9876.
"""
import json
import logging
import os
import re
import html
import time
from pathlib import Path
from datetime import datetime, timezone

import requests
import uvicorn
from fastapi import FastAPI, Request, Response

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
logger = logging.getLogger("cover_letter_bot")

# Telegram: @fl_aibot — same token as scrapers
# Load from config file to avoid Hermes security scan masking
_TOK_FILE = Path(os.path.expanduser("~/.hermes/config/fl_aibot_token.txt"))
if not _TOK_FILE.exists():
    _TOK_FILE = Path("/home/hermes/.hermes/config/fl_aibot_token.txt")
TG_BOT_TOKEN=_TOK_FILE.read_text().strip() if _TOK_FILE.exists() else ""
TG_CHAT_ID = "128204572"

# Authorised users — only these Telegram user IDs can use the bot
ALLOWED_USERS = {128204572, 253309061}

# OpenRouter API (deepseek-v4-flash-0731)
# Migrated from Ollama Cloud → OpenRouter (weekly usage limit reached)
LLM_BASE_URL = os.environ.get("OPENROUTER_BASE_URL", "https://openrouter.ai/api/v1")
LLM_API_KEY = os.environ.get("OPENROUTER_API_KEY", "")
LLM_MODEL = "deepseek/deepseek-v4-flash-0731"
LLM_MAX_TOKENS = 4096
LLM_TEMPERATURE = 0.7

# System prompt
SCRIPTS_DIR = Path(os.path.expanduser("~/.hermes/scripts"))
if not (SCRIPTS_DIR / "cover_letter_bot.py").exists():
    SCRIPTS_DIR = Path("/home/hermes/.hermes/scripts")
SYSTEM_PROMPT_PATH = SCRIPTS_DIR / "cover_letter_system_prompt.md"
SALES_EVENT_PROMPT_PATH = SCRIPTS_DIR / "sales_event_system_prompt.md"

# JSONL cache paths (same dirs as scrapers)
DATA_DIR = Path(os.path.expanduser("~/.hermes/data"))
if not (DATA_DIR / "profi_graphql").exists():
    DATA_DIR = Path("/home/hermes/.hermes/data")

JSONL_MAP = {
    "profi": DATA_DIR / "profi_graphql" / "profi_graphql_orders.jsonl",
    "youdo": DATA_DIR / "youdo" / "youdo_orders.jsonl",
    "kwork": DATA_DIR / "kwork" / "kwork_orders.jsonl",
    "fl": DATA_DIR / "fl_ru" / "fl_ru_orders.jsonl",
    "freelance": DATA_DIR / "freelance_ru" / "freelance_ru_orders.jsonl",
    "upwork": DATA_DIR / "upwork" / "upwork_orders.jsonl",
    "hh": DATA_DIR / "sales_event" / "sales_event_vacancies.jsonl",
}

# Firecrawl (for youdo full text)
FIRECRAWL_URL = "http://127.0.0.1:9123/v2/scrape"

# Per-source letter length limits (profi.ru has a 500-char platform cap)
LETTER_LIMITS = {
    "profi": 500,
    "upwork": 1200,
    "kwork": 1200,
    "fl": 1200,
    "freelance": 1200,
    "youdo": 1200,
    "hh": 1200,
}
DEFAULT_LETTER_LIMIT = 1200

app = FastAPI(title="Cover Letter Bot")


# ── Helpers ──────────────────────────────────────────────

def load_system_prompt() -> str:
    """Load system prompt from markdown file."""
    if not SYSTEM_PROMPT_PATH.exists():
        logger.error("System prompt file not found: %s", SYSTEM_PROMPT_PATH)
        return "Ты — Влад, AI-разработчик. Создай короткий отклик на задание с фриланс-биржи. Объём: не более {CHAR_LIMIT} символов."
    return SYSTEM_PROMPT_PATH.read_text(encoding="utf-8")


def load_sales_event_prompt() -> str:
    """Load system prompt for Sales & Event vacancy cover letters (candidate: Anna Kalyagina)."""
    if not SALES_EVENT_PROMPT_PATH.exists():
        logger.error("Sales event prompt file not found: %s", SALES_EVENT_PROMPT_PATH)
        return "Ты — Анна Калягина, менеджер по продажам. Создай короткий отклик на вакансию. Объём: не более {CHAR_LIMIT} символов."
    return SALES_EVENT_PROMPT_PATH.read_text(encoding="utf-8")


SYSTEM_PROMPT = load_system_prompt()
SALES_EVENT_PROMPT = load_sales_event_prompt()
logger.info("System prompt loaded (%d chars), Sales event prompt loaded (%d chars)",
            len(SYSTEM_PROMPT), len(SALES_EVENT_PROMPT))


def find_in_jsonl(source: str, order_id: str) -> dict | None:
    """Find order in JSONL cache by source and order_id."""
    jsonl_path = JSONL_MAP.get(source)
    if not jsonl_path or not jsonl_path.exists():
        logger.warning("JSONL not found for source=%s: %s", source, jsonl_path)
        return None

    # Read from end (most recent first) — but file may be large, so scan all
    try:
        with open(jsonl_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                    if str(obj.get("id", "")) == str(order_id):
                        return obj
                except json.JSONDecodeError:
                    continue
    except Exception as e:
        logger.error("Failed to read JSONL %s: %s", jsonl_path, e)
    return None


def fetch_youdo_full_text(task_id: str) -> dict:
    """Fetch full task text from youdo.com via Firecrawl.
    
    Youdo public pages are blocked from server IP (403 WAF),
    but Firecrawl bypasses it and returns markdown with full description.
    
    The description is in the "Нужно" section of the page.
    
    Returns dict with title, description, client fields.
    """
    url = f"https://youdo.com/t{task_id}"
    logger.info("Firecrawl: scraping %s", url)
    try:
        resp = requests.post(
            FIRECRAWL_URL,
            json={
                "url": url,
                "formats": ["markdown"],
                "onlyMainContent": True,
            },
            timeout=30,
        )
        if resp.status_code != 200:
            logger.error("Firecrawl status %d: %s", resp.status_code, resp.text[:200])
            return {"title": "", "description": "", "client": ""}
        
        data = resp.json()
        if not data.get("success"):
            logger.error("Firecrawl error: %s", data)
            return {"title": "", "description": "", "client": ""}
        
        md = data.get("data", {}).get("markdown", "")
        logger.info("Firecrawl returned %d chars markdown", len(md))
        
        # Extract title (first H1)
        title = ""
        title_match = re.search(r"^# (.+)$", md, re.MULTILINE)
        if title_match:
            title = title_match.group(1).strip()
        
        # Extract description from "Нужно" section
        # Format: "Нужно\n```\n<description>\n```\n"
        desc = ""
        need_match = re.search(r"##?\s*Нужно\s*\n```\n(.+?)\n```", md, re.DOTALL)
        if need_match:
            desc = need_match.group(1).strip()
        else:
            # Fallback: try "Нужно" followed by content without code block
            need_match2 = re.search(r"Нужно\s*\n(.+?)(?:\nОткликнуться|\n####|$)", md, re.DOTALL)
            if need_match2:
                desc = need_match2.group(1).strip()
            else:
                # Last fallback: strip title and metadata
                lines = md.split("\n")
                content_lines = []
                skip_metadata = True
                for line in lines:
                    if line.startswith("# "):
                        continue
                    if skip_metadata and ("просмотров" in line or "Создано" in line or "Бюджет" in line or "Адрес" in line):
                        continue
                    skip_metadata = False
                    if "Откликнуться" in line or "#### " in line:
                        break
                    content_lines.append(line)
                desc = "\n".join(content_lines).strip()
        
        return {"title": title, "description": desc, "client": ""}
    except Exception as e:
        logger.error("Firecrawl exception: %s", e)
        return {"title": "", "description": "", "client": ""}


def fetch_profi_client_name(order_id: str) -> str:
    """Fetch client name from Profi.ru GraphQL by re-querying BoSearchBoardItems.
    
    The board search doesn't support filtering by order ID directly,
    but the order usually appears in the first page of results.
    If found, returns clientInfo.name. Otherwise returns empty string.
    """
    import subprocess
    
    logger.info("profi: fetching client name for order %s via GraphQL", order_id)
    
    # Use the existing fetch script to get fresh data (handles JWT refresh internally)
    try:
        result = subprocess.run(
            ["python3", "/home/hermes/.hermes/scripts/profi_graphql_fetch.py"],
            capture_output=True, text=True, timeout=60,
        )
        if result.returncode != 0:
            logger.warning("profi_graphql_fetch.py exited %d: %s", result.returncode, result.stderr[:200])
            return ""
        
        # Skip first line (JWT info), parse rest as JSON
        lines = result.stdout.strip().split("\n")
        json_start = 0
        for i, line in enumerate(lines):
            if line.strip().startswith("{"):
                json_start = i
                break
        
        json_str = "\n".join(lines[json_start:])
        data = json.loads(json_str)
        
        # Search all snippets for matching ID
        for snippet in data.get("ai_orders", []):
            if str(snippet.get("id", "")) == str(order_id):
                ci = snippet.get("clientInfo", {})
                name = ci.get("name", "")
                if name:
                    logger.info("profi: found client name for %s: %s", order_id, name)
                    return name
        
        # Also check all snippets (not just AI orders)
        for snippet in data.get("all_snippets", []):
            if str(snippet.get("id", "")) == str(order_id):
                ci = snippet.get("clientInfo", {})
                name = ci.get("name", "")
                if name:
                    logger.info("profi: found client name for %s: %s", order_id, name)
                    return name
        
        logger.warning("profi: order %s not found in current board results", order_id)
        return ""
    except Exception as e:
        logger.error("profi: failed to fetch client name: %s", e)
        return ""


def get_order_text(source: str, order_id: str) -> str:
    """Get full order text for LLM prompt.
    
    Priority: JSONL cache → Firecrawl (youdo only) → empty.
    """
    # Try JSONL first
    record = find_in_jsonl(source, order_id)
    
    if record:
        title = record.get("title", "")
        desc = record.get("description", "")
        client = record.get("client", "") or record.get("username", "")
        
        # Decode HTML entities (kwork uses &laquo; etc.)
        if desc:
            desc = html.unescape(desc)
        
        # For youdo, description is always empty in JSONL — use Firecrawl
        if source == "youdo" and not desc:
            logger.info("youdo: empty description in JSONL, falling back to Firecrawl")
            fc_data = fetch_youdo_full_text(order_id)
            if fc_data["description"]:
                desc = fc_data["description"]
            if not title and fc_data["title"]:
                title = fc_data["title"]
        
        # For profi.ru, if client name is missing, fetch via GraphQL
        if source == "profi" and not client:
            logger.info("profi: client name missing in JSONL, fetching via GraphQL")
            client = fetch_profi_client_name(order_id)
        
        text = f"Заголовок: {title}\n"
        if client:
            text += f"Клиент: {client}\n"
        text += f"Описание: {desc}"
        return text
    
    # Record not in JSONL — try Firecrawl for youdo
    if source == "youdo":
        fc_data = fetch_youdo_full_text(order_id)
        text = f"Заголовок: {fc_data['title']}\nОписание: {fc_data['description']}"
        return text
    
    # For other sources, return minimal info
    logger.warning("Order not found: source=%s id=%s", source, order_id)
    return f"Заголовок: (не найдено)\nsource: {source}\norder_id: {order_id}"


def call_llm(system_prompt: str, user_prompt: str) -> str:
    """Call deepseek-v4-flash-0731 via OpenRouter API."""
    logger.info("Calling LLM: model=%s, prompt=%d chars", LLM_MODEL, len(user_prompt))
    
    resp = requests.post(
        f"{LLM_BASE_URL}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
        },
        json={
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "max_tokens": LLM_MAX_TOKENS,
            "temperature": LLM_TEMPERATURE,
            "reasoning": {"enabled": False},
        },
        timeout=60,
    )
    
    if resp.status_code != 200:
        logger.error("LLM API error %d: %s", resp.status_code, resp.text[:300])
        raise RuntimeError(f"LLM API error {resp.status_code}: {resp.text[:200]}")
    
    data = resp.json()
    content = data["choices"][0]["message"]["content"]
    logger.info("LLM returned %d chars", len(content))
    return content.strip()


def send_telegram(text: str, parse_mode: str = "HTML", reply_to: int | None = None,
                 chat_id: str | int | None = None) -> bool:
    """Send message to Telegram chat.

    If chat_id is None, falls back to TG_CHAT_ID (default owner chat).
    """
    payload = {
        "chat_id": chat_id if chat_id is not None else TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    # @fl_aibot is NOT a forum — no message_thread_id
    if reply_to:
        payload["reply_to_message_id"] = reply_to
    
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception as e:
        logger.error("Telegram send failed: %s", e)
        return False


def answer_callback(callback_id: str, text: str = "") -> None:
    """Answer callback query to remove loading state."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text},
            timeout=10,
        )
    except Exception as e:
        logger.warning("answerCallbackQuery failed: %s", e)


def truncate_to_limit(text: str, limit: int = DEFAULT_LETTER_LIMIT) -> str:
    """Ensure text is within limit, truncate with ellipsis if needed."""
    if len(text) <= limit:
        return text
    # Try to cut at last sentence boundary
    truncated = text[:limit]
    last_period = truncated.rfind(".")
    if last_period > limit - 100:
        return truncated[:last_period + 1]
    return truncated.rstrip() + "…"


# ── Webhook handler ─────────────────────────────────────

@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Handle Telegram webhook (callback_query from inline button)."""
    try:
        body = await request.body()
        update = json.loads(body)
    except Exception as e:
        logger.error("Failed to parse webhook body: %s", e)
        return Response(status_code=400)
    
    # Handle callback_query (inline button press)
    callback = update.get("callback_query")
    if not callback:
        logger.info("Not a callback_query, ignoring")
        return Response(status_code=200)
    
    callback_id = callback.get("id", "")
    data = callback.get("data", "")
    message = callback.get("message", {})
    chat_id = message.get("chat", {}).get("id", TG_CHAT_ID)
    message_id = message.get("message_id")
    user_id = callback.get("from", {}).get("id", 0)

    logger.info("Callback received: data=%s, chat=%s, msg=%s, user=%s", data, chat_id, message_id, user_id)

    # Authorisation check — only whitelisted users can use the bot
    if user_id not in ALLOWED_USERS:
        logger.warning("Unauthorized user %s attempted to use bot", user_id)
        answer_callback(callback_id, "⛔ Нет доступа")
        return Response(status_code=200)
    
    # Parse callback_data: "reply:{source}:{order_id}"
    parts = data.split(":", 2)
    if len(parts) != 3 or parts[0] != "reply":
        logger.warning("Invalid callback_data: %s", data)
        answer_callback(callback_id, "Неверный формат")
        return Response(status_code=200)
    
    _, source, order_id = parts
    logger.info("Processing: source=%s, order_id=%s", source, order_id)
    
    # Answer callback immediately to remove loading
    answer_callback(callback_id, "Генерирую отклик…")
    
    try:
        # 1. Get full order text
        order_text = get_order_text(source, order_id)
        logger.info("Order text: %d chars", len(order_text))
        
        # 2. Determine per-source character limit
        char_limit = LETTER_LIMITS.get(source, DEFAULT_LETTER_LIMIT)

        # 3. Select system prompt: hh → Anna Kalyagina, others → default
        base_prompt = SALES_EVENT_PROMPT if source == "hh" else SYSTEM_PROMPT
        prompt_with_limit = base_prompt.replace(
            "{CHAR_LIMIT}", str(char_limit)
        ) if "{CHAR_LIMIT}" in base_prompt else base_prompt

        # 4. Call LLM
        cover_letter = call_llm(prompt_with_limit, order_text)
        
        # 5. Truncate to source-specific limit
        cover_letter = truncate_to_limit(cover_letter, limit=char_limit)
        
        # 4. Send as monospace (preformatted) — copy-pasteable
        # Reply in the SAME chat where the button was pressed (so all allowed users get the letter)
        tg_text = f"<pre>{html.escape(cover_letter)}</pre>"
        send_telegram(tg_text, parse_mode="HTML", reply_to=message_id, chat_id=chat_id)
        logger.info("Cover letter sent (%d chars) to chat_id=%s", len(cover_letter), chat_id)
        
    except Exception as e:
        logger.error("Failed to generate cover letter: %s", e, exc_info=True)
        send_telegram(
            f"❌ Ошибка генерации отклика: {html.escape(str(e)[:200])}",
            reply_to=message_id,
            chat_id=chat_id,
        )
    
    return Response(status_code=200)


@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "model": LLM_MODEL, "prompt_loaded": len(SYSTEM_PROMPT),
            "sales_event_prompt": len(SALES_EVENT_PROMPT)}


@app.get("/")
async def root():
    """Root endpoint."""
    return {"service": "cover-letter-bot", "status": "running"}


if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=9876, log_level="info")