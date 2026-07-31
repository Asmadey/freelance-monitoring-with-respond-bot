#!/usr/bin/env python3
"""
Freelance AI Projects Daily Monitor — Aggregated Report
Runs fl.ru, freelance.ru, kwork.ru scrapers and sends a single HTML-formatted Telegram message.

Usage: python3 freelance_ai_daily_report.py
"""
import html
import json
import os
import re
import subprocess
import sys
from datetime import datetime

import requests

SPREADSHEET_ID = "1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok"
MASTER_SHEET_URL = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/edit"

# Absolute path — cron no_agent sets HOME=/home/hermes/.hermes/home, breaking ~ expansion
PYTHON = "/home/hermes/.hermes/venvs/freelance_monitor/bin/python3"
if not os.path.exists(PYTHON):
    PYTHON = os.path.expanduser("~/.hermes/venvs/freelance_monitor/bin/python3")


def load_env():
    for env_path in (
        "/home/hermes/.hermes/.env",
        os.path.expanduser("~/.hermes/.env"),
    ):
        if os.path.exists(env_path):
            break
    else:
        env_path = None
    if env_path and os.path.exists(env_path):
        with open(env_path) as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID") and key not in os.environ:
                    os.environ[key] = value.strip().strip('"').strip("'")


load_env()
TELEGRAM_BOT_TOKEN="8776532572:AAGh2OnHOaUjZAs-M-04nluayq2-qM4O8fk"  # @fl_aibot
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()

SCRIPTS_DIR = "/home/hermes/.hermes/scripts"
if not os.path.isdir(SCRIPTS_DIR):
    SCRIPTS_DIR = os.path.expanduser("~/.hermes/scripts")

SCRIPTS = {
    "fl.ru": {
        "cmd": [PYTHON, f"{SCRIPTS_DIR}/fl_ru_scraper.py"],
        "sheet_gid": "282555607",
    },
    "freelance.ru": {
        "cmd": [PYTHON, f"{SCRIPTS_DIR}/freelance_ru_scraper.py"],
        "sheet_gid": "300837051",
    },
    "kwork.ru": {
        "cmd": [PYTHON, f"{SCRIPTS_DIR}/kwork_ru_scraper.py"],
        "sheet_gid": "2097667003",
    },
}

LINE_RE = re.compile(
    r"\[(?P<source>fl\.ru|freelance\.ru|kwork\.ru) Scraper\] "
    r"(?P<metric>Scraped|Total projects|Relevant|New jobs):?\s*(?P<value>\d+)"
)
KWORK_RELEVANT_RE = re.compile(
    r"\[kwork\.ru Scraper\] Relevant \(keyword \+ LLM\): (?P<value>\d+)"
)
SKIP_RE = re.compile(
    r"\[(?P<source>fl\.ru|freelance\.ru|kwork\.ru)\] Skipped (?P<count>\d+) jobs older than 24h"
)


def run_scraper(name, cfg):
    result = {
        "status": "ok",
        "total": 0,
        "relevant": 0,
        "new": 0,
        "skipped_old": 0,
        "error": None,
        "top": [],
        "sheet_url": f"{MASTER_SHEET_URL}#gid={cfg['sheet_gid']}",
        "raw_report": "",
    }

    try:
        proc = subprocess.run(
            cfg["cmd"],
            cwd="/home/hermes",
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        result["status"] = "timeout"
        result["error"] = "Timeout after 300s"
        return result
    except Exception as e:
        result["status"] = "error"
        result["error"] = str(e)
        return result

    # Treat non-zero exit as error unless we still got a report
    if proc.returncode != 0 and not proc.stdout.strip():
        result["status"] = "error"
        result["error"] = f"exit {proc.returncode}: {proc.stderr[-500:]}"
        return result

    # DEBUG: capture full stderr to local log
    debug_log = f"/home/hermes/.hermes/logs/{name}_scraper.debug.log"
    if not os.path.isdir(os.path.dirname(debug_log)):
        debug_log = os.path.expanduser(f"~/.hermes/logs/{name}_scraper.debug.log")
    os.makedirs(os.path.dirname(debug_log), exist_ok=True)
    with open(debug_log, "w") as fh:
        fh.write(f"EXIT: {proc.returncode}\n")
        fh.write(f"STDOUT:\n{proc.stdout}\n")
        fh.write(f"STDERR:\n{proc.stderr}\n")

    # Parse structured metrics from stderr
    for line in proc.stderr.splitlines():
        m = LINE_RE.match(line)
        if m:
            src, metric, value = m.group("source"), m.group("metric"), int(m.group("value"))
            if metric == "Scraped" or (src == "kwork.ru" and metric == "Total projects"):
                result["total"] = value
            elif metric == "Relevant" and src != "kwork.ru":
                result["relevant"] = value
            elif metric == "New jobs":
                result["new"] = value
        km = KWORK_RELEVANT_RE.match(line)
        if km:
            result["relevant"] = int(km.group("value"))
        sm = SKIP_RE.match(line)
        if sm:
            result["skipped_old"] = int(sm.group("count"))

    # The scrapers output Telegram report on stdout
    result["raw_report"] = proc.stdout.strip()

    # Extract top-5 job entries from raw report for aggregation
    result["top"] = extract_top_jobs(result["raw_report"])

    return result


def extract_top_jobs(report):
    jobs = []
    lines = report.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i].strip()
        # scraper stdout uses Telegram Markdown: "1. *Title* | [ссылка](URL)"
        # or legacy "1. *Title*"
        m = re.match(
            r"^(\d+)\.\s*\*(.+?)\*\s*(?:\|\s*\[ссылка\]\((https?://[^\s)]+)\))?\s*$",
            line,
        )
        if m:
            idx = int(m.group(1))
            title = m.group(2).strip()
            link = m.group(3) or ""
            meta = ""
            desc = ""
            if i + 1 < len(lines):
                meta = lines[i + 1].strip()
            if i + 2 < len(lines) and lines[i + 2].strip().startswith("📝"):
                desc = lines[i + 2].strip()
                i += 1
            jobs.append({"title": title, "link": link, "meta": meta, "desc": desc})
            i += 1
        i += 1
        if len(jobs) >= 5:
            break
    return jobs


def send_telegram(text: str) -> dict:
    """Send an HTML-formatted message to Telegram Bot API."""
    if not TELEGRAM_BOT_TOKEN:
        print("ERROR: TELEGRAM_BOT_TOKEN not set; skipping Telegram send.", file=sys.stderr)
        return {"error": "missing token"}
    if not TELEGRAM_CHAT_ID:
        print("ERROR: TELEGRAM_CHAT_ID not set; skipping Telegram send.", file=sys.stderr)
        return {"error": "missing chat_id"}

    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text,
        "parse_mode": "HTML",
        "disable_web_page_preview": True,
    }

    try:
        resp = requests.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        if not data.get("ok"):
            print(f"Telegram API error: {data}", file=sys.stderr)
        return data
    except Exception as e:
        print(f"ERROR sending Telegram message: {e}", file=sys.stderr)
        return {"error": str(e)}


def h(text: str) -> str:
    """Escape HTML special chars."""
    return html.escape(str(text)) if text else ""


def format_table_row(name, data):
    status_emoji = "🟢" if data["status"] == "ok" else "🔴"
    return (
        f"{status_emoji} <b>{h(name)}</b>: "
        f"всего {data['total']} | релевантных {data['relevant']} | "
        f"новых <b>{data['new']}</b> | старше 24ч {data['skipped_old']}"
    )


def build_report(results):
    today_str = datetime.now().strftime("%d.%m.%Y")
    total_new = sum(r["new"] for r in results.values() if r["status"] == "ok")

    lines = [
        f'<a href="{h(MASTER_SHEET_URL)}">📊 Общая таблица</a>',
        "",
        f"<i>Freelance AI Daily Monitor: {h(today_str)}</i>",
        f"Всего новых находок: <b>{total_new}</b>",
        "",
        "<b>Статус по источникам</b>",
        "",
    ]

    for name, data in results.items():
        lines.append(format_table_row(name, data))

    # Errors section
    errors = [(n, d) for n, d in results.items() if d["status"] != "ok"]
    if errors:
        lines.extend(["", "<b>Ошибки</b>"])
        for name, data in errors:
            lines.append(f"🔴 <b>{h(name)}</b>: {h(data.get('error', 'unknown error'))}")

    # Aggregated top findings from all sources
    all_top = []
    for name, data in results.items():
        for j in data["top"]:
            all_top.append((name, j))

    if all_top:
        lines.extend(["", f"🔥 <b>ТОП находки</b> ({len(all_top)})"])
        for i, (source, job) in enumerate(all_top, 1):
            sheet_url = results[source]["sheet_url"]
            title = h(job["title"])
            project_url = job["link"]
            if project_url:
                title_link = f'<a href="{h(project_url)}">{title}</a>'
            else:
                title_link = title
            lines.append(
                f"{i}. {title_link} | <a href=\"{h(sheet_url)}\">{h(source)}</a>"
            )
            if job["meta"]:
                lines.append(f"   {h(job['meta'])}")
            if job["desc"]:
                lines.append(f"   {h(job['desc'])}")

    return "\n".join(lines)


def main():
    results = {}
    for name, cfg in SCRIPTS.items():
        results[name] = run_scraper(name, cfg)

    report = build_report(results)

    # Send to Telegram with HTML formatting
    send_result = send_telegram(report)

    # If Telegram send failed or chat_id not configured, fall back to stdout
    if not send_result.get("ok"):
        print(report)

    # Also emit a machine-readable summary to stderr for cron logging
    summary = {
        "date": datetime.now().strftime("%d.%m.%Y"),
        "sources": {
            name: {
                "status": d["status"],
                "total": d["total"],
                "relevant": d["relevant"],
                "new": d["new"],
                "skipped_old": d["skipped_old"],
            }
            for name, d in results.items()
        },
    }
    print(json.dumps(summary, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
