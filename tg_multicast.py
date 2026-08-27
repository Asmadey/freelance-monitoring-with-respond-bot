"""
Shared Telegram multicast helper for freelance scraper bots (@fl_aibot).

Single source of truth for chat IDs: ~/.hermes/fl-aibot-chats.json
All scrapers that send vacancy cards with "Написать отклик" buttons
import from this module to ensure every authorised user receives cards.

Usage in a scraper:

    from tg_multicast import get_chat_ids, send_multicast

    chat_ids = get_chat_ids()           # list of int/str chat IDs
    send_multicast(TG_BOT_TOKEN, chat_ids, text, parse_mode="HTML",
                   reply_markup=keyboard, tag="fl.ru")
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Sequence

CHAT_IDS_FILE = os.path.expanduser("~/.hermes/fl-aibot-chats.json")

# Fallback if file doesn't exist yet — only Vlad
_FALLBACK_CHAT_IDS = [128204572]


def get_chat_ids(filepath: str = CHAT_IDS_FILE) -> list:
    """Load chat IDs from JSON file. Returns fallback list if file missing."""
    p = Path(filepath)
    if not p.exists():
        print(f"[tg_multicast] Chat IDs file not found: {filepath}, "
              f"using fallback {_FALLBACK_CHAT_IDS}", file=sys.stderr)
        return list(_FALLBACK_CHAT_IDS)
    try:
        with open(p, encoding="utf-8") as fh:
            ids = json.load(fh)
        if not isinstance(ids, list) or not ids:
            return list(_FALLBACK_CHAT_IDS)
        return ids
    except Exception as e:
        print(f"[tg_multicast] Failed to load chat IDs: {e}", file=sys.stderr)
        return list(_FALLBACK_CHAT_IDS)


def _remove_blocked(filepath: str, blocked: set) -> None:
    """Remove blocked chat IDs from the JSON file."""
    if not blocked:
        return
    try:
        with open(filepath, encoding="utf-8") as fh:
            ids = json.load(fh)
        remaining = [c for c in ids if c not in blocked]
        if len(remaining) != len(ids):
            with open(filepath, "w", encoding="utf-8") as fh:
                json.dump(remaining, fh, ensure_ascii=False, indent=2)
            print(f"[tg_multicast] Removed blocked chat_ids: {blocked}",
                  file=sys.stderr)
    except Exception as e:
        print(f"[tg_multicast] Failed to update chat ids file: {e}",
              file=sys.stderr)


def send_multicast(
    bot_token: str,
    chat_ids: Sequence,
    text: str,
    parse_mode: str = "HTML",
    reply_markup: dict | None = None,
    tag: str = "",
    max_chars: int = 4000,
    timeout: int = 20,
    disable_preview: bool = True,
) -> bool:
    """Send a message to ALL chat IDs. Returns True if at least one succeeded.

    Automatically removes chat IDs that blocked the bot (403).
    """
    if not bot_token or not chat_ids:
        return False

    if len(text) > max_chars:
        text = text[:max_chars] + "…"

    tag_prefix = f"[{tag}] " if tag else ""
    blocked: set = set()
    any_success = False

    for chat_id in chat_ids:
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode,
            "disable_web_page_preview": disable_preview,
        }
        # No thread_id — @fl_aibot sends to private chats, not forum topics
        # (670423 is a thread in @hermesvladbot forum, @fl_aibot can't write there)
        if reply_markup:
            payload["reply_markup"] = reply_markup

        data = json.dumps(payload).encode()
        req = urllib.request.Request(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            data=data,
            method="POST",
        )
        req.add_header("Content-Type", "application/json")

        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                result = json.load(resp)
            if result.get("ok"):
                any_success = True
            else:
                print(f"{tag_prefix}Telegram send to {chat_id} failed: "
                      f"{result.get('description', '?')}", file=sys.stderr)
        except urllib.error.HTTPError as e:
            err_body = e.read().decode()[:500]
            print(f"{tag_prefix}Telegram HTTP {e.code} to {chat_id}: "
                  f"{err_body}", file=sys.stderr)
            if e.code == 403 and "blocked" in err_body.lower():
                blocked.add(chat_id)
        except Exception as e:
            print(f"{tag_prefix}Telegram error to {chat_id}: {e}",
                  file=sys.stderr)

    if blocked:
        _remove_blocked(CHAT_IDS_FILE, blocked)

    return any_success