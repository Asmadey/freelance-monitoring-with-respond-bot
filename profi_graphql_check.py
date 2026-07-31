#!/usr/bin/env python3
"""
Profi.ru GraphQL order monitor with Telegram notifications.
Runs every 5 minutes, deduplicates by order ID, sends AI matches to Telegram.
"""
import json
import logging
import os
import re
import subprocess
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
logger = logging.getLogger("profi_graphql_check")

# Telegram: @fl_aibot (Freelance Jobs bot)
TG_BOT_TOKEN = "8776532572:AAGh2OnHOaUjZAs-M-04nluayq2-qM4O8fk"
TG_CHAT_ID = "128204572"

SOCKS_PROXY = {"http": "socks5h://127.0.0.1:10808", "https": "socks5h://127.0.0.1:10808"}
NO_PROXY = None  # direct connection

def get_proxy():
    """Return working proxy dict, or None for direct connection.
    Tests SOCKS5 proxy with a quick TCP connect; falls back to direct if unavailable.
    """
    import socket as _sock
    try:
        s = _sock.create_connection(("127.0.0.1", 10808), timeout=3)
        s.close()
        return SOCKS_PROXY
    except OSError:
        logger.warning("SOCKS5 proxy unavailable, using direct connection")
        return NO_PROXY

PROXY = get_proxy()
GRAPHQL_URL = "https://api.profi.ru/warp/v2/graphql"

DATA_DIR = Path(os.path.expanduser("~/.hermes/data/profi_graphql"))
if not (DATA_DIR / "BoSearchBoardItems_query.graphql").exists():
    DATA_DIR = Path("/home/hermes/.hermes/data/profi_graphql")
DATA_DIR.mkdir(parents=True, exist_ok=True)
ERROR_ALERT_PATH = DATA_DIR / "profi_graphql_error_alerts.json"


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


SEEN_IDS_PATH = DATA_DIR / "profi_graphql_seen_ids.json"
OUT_JSONL = DATA_DIR / "profi_graphql_orders.jsonl"

QUERY = (DATA_DIR / "BoSearchBoardItems_query.graphql").read_text()
VARIABLES = json.loads((DATA_DIR / "BoSearchBoardItems_variables.json").read_text())

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
# Word boundary for Cyrillic: use lookarounds (\b doesn't work with Cyrillic)
AI_RE_SHORT = re.compile(
    r"(?i)(?<![a-zа-яё])(?:"
    + "|".join(re.escape(k) for k in AI_KEYWORDS_SHORT)
    + r")(?![a-zа-яё])"
)


def is_ai_match(text: str) -> bool:
    """Check if text contains any AI keyword (with proper word boundaries for short ones)."""
    return bool(AI_RE_LONG.search(text) or AI_RE_SHORT.search(text))


def load_seen_ids() -> set[str]:
    if not SEEN_IDS_PATH.exists():
        return set()
    try:
        data = json.loads(SEEN_IDS_PATH.read_text(encoding="utf-8"))
        return set(str(x) for x in data if x)
    except Exception:
        logger.exception("Failed to load seen_ids")
        return set()


def save_seen_ids(ids: set[str]) -> None:
    SEEN_IDS_PATH.write_text(
        json.dumps(sorted(ids), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def append_jsonl(obj: dict) -> None:
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def load_storage():
    ss = Path(os.path.expanduser("~/.hermes/secrets/profi_storage_state.json"))
    if not ss.exists():
        ss = Path("/home/hermes/.hermes/secrets/profi_storage_state.json")
    if not ss.exists():
        return {}
    return json.loads(ss.read_text())


def load_jwt():
    token = os.environ.get("PROFI_BO_JWT")
    if token:
        return token
    data = load_storage()
    for c in data.get("cookies", []):
        if c["name"] == "prfr_bo_tkn":
            return c["value"]
    logger.warning("No JWT found; will attempt silent refresh")
    raise RuntimeError("No JWT found")


def load_cookies():
    data = load_storage()
    return {c["name"]: c["value"] for c in data.get("cookies", []) if c["name"] in ("uid", "sid", "sl-session")}


def build_headers(jwt):
    return {
        "Host": "api.profi.ru",
        "x-app-id": "BO",
        "Accept": "application/json",
        "x-warp-ui-ver": "1.152.2",
        "x-warp-ui-app": "RNMOBBO",
        "Authorization": f"JWT {jwt}",
        "x-warp-consumer": "MOBILE",
        "Accept-Language": "ru",
        "x-warp-ui-type": "IOS",
        "x-new-auth-compatible": "1",
        "User-Agent": "rbo/261981023 CFNetwork/3860.600.12 Darwin/25.5.0",
        "Content-Type": "application/json",
    }


def fetch_orders(jwt):
    headers = build_headers(jwt)
    cookies = load_cookies()
    resp = requests.post(
        GRAPHQL_URL,
        headers=headers,
        cookies=cookies,
        json={"query": QUERY, "variables": VARIABLES},
        proxies=PROXY,
        timeout=30,
    )
    if resp.status_code in (401, 403):
        raise RuntimeError(f"GraphQL auth failed: {resp.status_code} {resp.text[:200]}")
    resp.raise_for_status()
    return resp.json()


def extract_snippets(payload):
    items = payload.get("data", {}).get("boSearchBoardItems", {}).get("items", [])
    return [it for it in items if it.get("type") == "SNIPPET"]


def is_ai_order(snippet):
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}".lower()
    return is_ai_match(text)


def extract_price(snippet: dict) -> str:
    price = snippet.get("price") or {}
    if price.get("value"):
        parts = [price.get("prefix", ""), price.get("value", ""), price.get("suffix", "")]
        return " ".join(p for p in parts if p).strip()
    second = snippet.get("secondPrice") or {}
    if second.get("value"):
        parts = [second.get("prefix", ""), second.get("value", ""), second.get("suffix", "")]
        return " ".join(p for p in parts if p).strip()
    return ""


def extract_location(snippet: dict) -> str:
    geo = snippet.get("geo") or {}
    if geo.get("remote"):
        r = geo["remote"]
        addr = r.get("address", "")
        prefix = r.get("prefix", "")
        return f"{prefix}, {addr}".strip(", ")
    order_loc = geo.get("orderLocation") or {}
    return order_loc.get("title", "")


def extract_client_name(snippet: dict) -> str:
    # GraphQL field: snippet.clientInfo.name (BoSearchSnippet) — usually null
    client_info = snippet.get("clientInfo") or {}
    name = client_info.get("name", "") or ""
    if name:
        return name
    # Fallback: legacy header.clientName
    header = snippet.get("header") or {}
    return header.get("clientName", "") or ""


def fetch_client_name(order_id: str, jwt: str = None, cookies: dict = None, timeout: int = 10) -> str:
    """Fetch client name from Profi.ru mobile backoffice API (getKlientInfo).
    GraphQL BoSearchBoardItems returns clientInfo.name=null, so we need this separate call.
    Returns empty string on failure (non-fatal).
    """
    try:
        if jwt is None or cookies is None:
            cookies = load_cookies()
            jwt = load_jwt()
        if not jwt:
            return ""
        request_body = {
            "meta": {
                "method": "getKlientInfo",
                "store_src": "",
                "ui_type": "IOS",
                "ui_app": "RNMOBBO",
                "ui_ver": "1.152.2",
                "ui_os": "26.5.2",
                "codepush_version": "0b77103a-da05-3aef-8338-c57ef2fcd9d0",
                "native_version": "1.152.0",
            },
            "data": {"order_id": str(order_id)},
        }
        files = {"request": (None, json.dumps(request_body), "application/json")}
        headers = build_headers(jwt)
        headers.pop("Content-Type", None)  # multipart boundary set by requests
        resp = requests.post(
            "https://api.profi.ru/mobile/backoffice/v2/",
            headers=headers,
            cookies=cookies,
            files=files,
            proxies=PROXY,
            timeout=timeout,
        )
        if resp.status_code != 200:
            return ""
        data = resp.json()
        return data.get("data", {}).get("name", "") or ""
    except Exception as e:
        logger.debug(f"fetch_client_name error for {order_id}: {e}")
        return ""


def snippet_tags(snippet: dict) -> list[str]:
    text = f"{snippet.get('title', '')} {snippet.get('description', '')}".lower()
    tags = []
    tag_map = [
        ("#ai", ["ai", "artificial intelligence", "ии"]),
        ("#нейросети", ["нейросет", "нейронная сеть", "neural"]),
        ("#chatgpt", ["chatgpt"]),
        ("#automation", ["automation", "автоматизация"]),
        ("#n8n", ["n8n"]),
        ("#promt", ["промт", "prompt"]),
        ("#data", ["data science", "анализ данных", "data analysis"]),
        ("#llm", ["llm", "large language model"]),
        ("#bot", ["bot", "бот"]),
    ]
    for tag, kws in tag_map:
        if any(kw in text for kw in kws):
            tags.append(tag)
    return tags


def order_url(snippet: dict) -> str:
    return f"https://profi.ru/backoffice/n.php?o={snippet['id']}"


def format_order(snippet: dict, fetched_client: str = "") -> str:
    title = snippet.get("title", "Без названия")
    price = extract_price(snippet)
    location = extract_location(snippet)
    client = extract_client_name(snippet) or fetched_client
    desc = snippet.get("description", "")
    if len(desc) > 300:
        desc = desc[:300] + "…"
    url = order_url(snippet)

    lines = ["#Profi"]
    lines.append("")
    lines.append(f"<b>{title}</b>")
    lines.append("")
    if price:
        lines.append(f"💰 {price}")
    if location:
        lines.append(f"📍 {location}")
    if client:
        lines.append(f"👤 {client}")
    if desc:
        lines.append(f"📝 {desc}")
    lines.append("")
    lines.append(f"🔗 <a href=\"{url}\">{snippet['id']}</a>")
    return "\n".join(lines)


def send_telegram(text: str, parse_mode: str = "HTML", reply_markup: dict | None = None) -> bool:
    if not TG_BOT_TOKEN or not TG_CHAT_ID:
        logger.warning("Telegram credentials not set")
        return False
    payload = {
        "chat_id": TG_CHAT_ID,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": True,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    try:
        resp = requests.post(
            f"https://api.telegram.org/bot{TG_BOT_TOKEN}/sendMessage",
            json=payload,
            timeout=20,
        )
        resp.raise_for_status()
        return True
    except Exception:
        logger.exception("Telegram send failed")
        return False


def cover_letter_keyboard(order_id: str) -> dict:
    """Inline keyboard with 'Написать отклик' button."""
    return {
        "inline_keyboard": [[
            {"text": "📝 Написать отклик", "callback_data": f"reply:profi:{order_id}"}
        ]]
    }


def run_auth_refresh(max_attempts=2, retry_delay=5):
    """Run profi_auth_refresh.py renew, with retry on failure (e.g. 423 mutex locked)."""
    script = Path(os.path.expanduser("~/.hermes/scripts/profi_auth_refresh.py"))
    if not script.exists():
        script = Path("/home/hermes/.hermes/scripts/profi_auth_refresh.py")
    for attempt in range(max_attempts):
        logger.info("JWT expired / missing, running silent refresh (attempt %d/%d)", attempt + 1, max_attempts)
        proc = subprocess.run(
            [sys.executable, str(script), "renew"],
            capture_output=True,
            text=True,
            timeout=120,
        )
        if proc.returncode == 0:
            logger.info("Silent refresh succeeded")
            return True
        logger.error("Silent refresh failed (attempt %d): %s", attempt + 1, proc.stderr or proc.stdout)
        if attempt < max_attempts - 1:
            logger.info("Retrying refresh in %ds...", retry_delay)
            time.sleep(retry_delay)
    return False


def is_network_error_str(err_str: str) -> bool:
    """Detect network-level errors (proxy down, TLS, timeout) vs auth errors."""
    err_lower = err_str.lower()
    return any(kw in err_lower for kw in ("ssl", "eof", "connection", "timeout", "refused", "socks", "max retries"))


# After daily reboot (02:00 MSK), Xray needs ~30-60s to start accepting connections.
# Retry network-dependent operations instead of alerting immediately.
NETWORK_RETRY_ATTEMPTS = 3
NETWORK_RETRY_DELAY = 15  # seconds


def fetch_orders_with_refresh():
    """Try GraphQL with current JWT; if 401/403, run silent refresh and retry once."""
    for attempt in range(2):
        jwt = load_jwt()
        try:
            return fetch_orders(jwt)
        except RuntimeError as e:
            if "GraphQL auth failed" in str(e) and attempt == 0:
                logger.info("GraphQL auth error; attempting silent refresh")
                if run_auth_refresh():
                    continue
                else:
                    logger.error("Silent refresh failed during retry")
            raise
    raise RuntimeError("GraphQL auth failed after refresh")


def main() -> int:
    try:
        logger.info("Starting Profi.ru GraphQL order check")
        seen_ids = load_seen_ids()
        logger.info("Loaded %d seen ids", len(seen_ids))
    except Exception as e:
        logger.error("Failed to initialize: %s", e)
        return 1

    # Step 1: Always renew JWT first (replaces separate JWT refresh cron)
    jwt = None
    try:
        jwt = load_jwt()
    except RuntimeError:
        pass  # will try refresh below

    # Proactive refresh: always touch JWT before fetch to avoid 401 mid-request
    # Retry on network errors (Xray may be starting up after daily reboot at 02:00 MSK)
    logger.info("Proactive JWT refresh before fetch")
    refresh_ok = False
    for net_attempt in range(NETWORK_RETRY_ATTEMPTS):
        if run_auth_refresh():
            refresh_ok = True
            break
        if net_attempt < NETWORK_RETRY_ATTEMPTS - 1:
            logger.warning("Proactive refresh failed (attempt %d/%d), retrying in %ds",
                           net_attempt + 1, NETWORK_RETRY_ATTEMPTS, NETWORK_RETRY_DELAY)
            time.sleep(NETWORK_RETRY_DELAY)

    if not refresh_ok:
        logger.warning("Proactive refresh failed after %d attempts, will try fetch with existing JWT", NETWORK_RETRY_ATTEMPTS)
        if jwt is None:
            error_key = "jwt_missing"
            if not was_error_alerted_recently(error_key, cooldown_seconds=3600):
                send_telegram(
                    f"⚠️ <b>[profi.ru]</b>\n\nНе найден JWT и не удалось обновить ({NETWORK_RETRY_ATTEMPTS} попыток). Запустите вручную:\n"
                    "<code>python3 ~/.hermes/scripts/profi_auth_refresh.py renew</code>"
                )
                mark_error_alerted(error_key)
            return 1
    else:
        # Reload fresh JWT after successful renew
        try:
            jwt = load_jwt()
        except RuntimeError:
            logger.error("JWT still missing after successful renew")
            return 1

    # Step 2: Fetch orders (with retry-on-401 as backup)
    # Network errors (proxy down after reboot, TLS) get retried with delay before alerting.
    payload = None
    last_err = None
    for net_attempt in range(NETWORK_RETRY_ATTEMPTS):
        try:
            payload = fetch_orders_with_refresh()
            break
        except Exception as e:
            err_str = str(e)
            last_err = e
            if is_network_error_str(err_str) and net_attempt < NETWORK_RETRY_ATTEMPTS - 1:
                logger.warning(
                    "Network error (attempt %d/%d), retrying in %ds: %s",
                    net_attempt + 1, NETWORK_RETRY_ATTEMPTS, NETWORK_RETRY_DELAY, err_str[:150],
                )
                time.sleep(NETWORK_RETRY_DELAY)
                continue
            # Non-network error or last attempt — fall through to alert logic
            logger.error("GraphQL fetch failed: %s", err_str)
            error_key = err_str[:50]
            if not was_error_alerted_recently(error_key):
                if is_network_error_str(err_str):
                    send_telegram(
                        f"⚠️ <b>[profi.ru]</b>\n\nСетевая ошибка (VPN/TLS) — {NETWORK_RETRY_ATTEMPTS} retry не помогли:\n"
                        f"<code>{err_str[:200]}</code>\n\n"
                        "Проверьте Xray: `systemctl status xray-profi.service`"
                    )
                else:
                    send_telegram(
                        f"⚠️ <b>[profi.ru]</b>\n\nОшибка запроса заказов:\n"
                        f"<code>{err_str[:200]}</code>\n\n"
                        "Возможно, сессия протухла. Попробуйте обновить:\n"
                        "<code>python3 ~/.hermes/scripts/profi_auth_refresh.py renew</code>"
                    )
                mark_error_alerted(error_key)
            return 1

    if payload is None:
        logger.error("GraphQL fetch failed after all retries: %s", last_err)
        return 1

    snippets = extract_snippets(payload)
    logger.info("Fetched %d snippets", len(snippets))

    new_ai_orders = []
    jwt = load_jwt()
    cookies = load_cookies()
    for s in snippets:
        oid = str(s.get("id", ""))
        if not oid:
            continue
        if oid in seen_ids:
            continue
        seen_ids.add(oid)

        # Fetch client name from mobile backoffice API (GraphQL returns null)
        client_name = extract_client_name(s)
        if not client_name:
            client_name = fetch_client_name(oid, jwt=jwt, cookies=cookies)
            if client_name:
                logger.info(f"  Client for {oid}: {client_name}")

        record = {
            "id": oid,
            "title": s.get("title", ""),
            "description": s.get("description", ""),
            "price": extract_price(s),
            "location": extract_location(s),
            "client": client_name,
            "tags": snippet_tags(s),
            "source": "profi.ru",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        }
        append_jsonl(record)

        if is_ai_order(s):
            s["_fetched_client"] = client_name
            new_ai_orders.append(s)

    save_seen_ids(seen_ids)

    if new_ai_orders:
        logger.info("Found %d new AI orders", len(new_ai_orders))
        for s in new_ai_orders:
            oid = str(s.get("id", ""))
            fetched_client = s.pop("_fetched_client", "")
            send_telegram(format_order(s, fetched_client=fetched_client), reply_markup=cover_letter_keyboard(oid))
            time.sleep(0.5)
    else:
        logger.info("No new AI orders from profi.ru")

    return 0


if __name__ == "__main__":
    sys.exit(main())
