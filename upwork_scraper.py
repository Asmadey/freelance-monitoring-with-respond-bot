#!/usr/bin/env python3
"""
Upwork → Telegram: Firecrawl scraper + parser + dedup.
Saves raw JSON per query to /tmp/upwork_*.json, then parses.
State: /tmp/upwork_seen_urls.json
Saves orders to JSONL for cover letter bot lookup.
Sends directly to Telegram with inline "Написать отклик" button.
"""
import json, re, os, sys, html, time
from datetime import datetime, timezone, timedelta
from pathlib import Path
import urllib.parse
from urllib.request import Request, urlopen
import requests
from urllib.error import URLError, HTTPError

FIRECRAWL_URL = "http://127.0.0.1:9123/v2/scrape"
FIRECRAWL_FALLBACK_URL = "https://api.firecrawl.dev/v2/scrape"
FIRECRAWL_FALLBACK_KEY = os.environ.get("FIRECRAWL_API_KEY", "").strip() or os.environ.get("FIRECRAWL_KEY_1", "").strip() or os.environ.get("FIRECRAWL_KEY_2", "").strip() or os.environ.get("FIRECRAWL_KEY_3", "").strip() or os.environ.get("FIRECRAWL_KEY_4", "").strip() or os.environ.get("FIRECRAWL_KEY_5", "").strip()
STATE_FILE = "/home/hermes/.hermes/upwork_seen_urls.json"
SEEN_EXPIRE_HOURS = 720  # 30 days

# Telegram: @fl_aibot (Freelance Jobs bot) — same token as other scrapers
# 2026-09-03 fix: profi_graphql_check.py no longer hardcodes the token as a string
# (it reads it from the token file now), so regex extraction broke and silently
# produced an empty token → all TG sends failed with False. Read the file directly.
_TF = "/home/hermes/.hermes/config/fl_aibot_token.txt"
try:
    TG_BOT_TOKEN = open(_TF).read().strip()
except OSError:
    TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")

# Multi-chat: send to all authorised users via shared helper
import tg_multicast
TG_CHAT_IDS = tg_multicast.get_chat_ids()

# JSONL cache for cover letter bot
DATA_DIR = Path("/home/hermes/.hermes/data/upwork")
DATA_DIR.mkdir(parents=True, exist_ok=True)
OUT_JSONL = DATA_DIR / "upwork_orders.jsonl"

QUERIES = [
    "AI agent automation n8n",
    "Python bot web scraping automation",
    "Zapier Make no-code integration",
    "AI agent development LangChain",
    "LLM integration OpenAI Claude",
    "LangChain LangGraph agentic workflow",
    "AI workflow automation no-code",
]

KEYWORDS = [
    "ai", "automation", "python", "bot", "scraping", "n8n", "make",
    "zapier", "no-code", "nocode", "agent", "llm", "langchain",
    "langgraph", "openai", "claude", "webhook", "api integration",
    "workflow", "google sheets", "gsuite", "lead generation",
    "crm", "data extraction", "parsing", "scraping"
]

# Location markers that indicate the client restricts applications to US freelancers.
# These are matched case-insensitively against the raw markdown block.
# NOTE: the raw markdown keeps the unicode bullet "Remote • United States" as a location
# line for US-only jobs, so "remote • united states" must be treated as a US-only marker.
US_LOCATION_MARKERS = [
    "only freelancers located in the u.s.",
    "only freelancers located in the us",
    "only freelancers located in the united states",
    "freelancers located in the u.s.",
    "freelancers located in the us",
    "freelancers located in the united states",
    "must be based in u.s.",
    "must be based in us",
    "must be based in the united states",
    "based in u.s.",
    "based in us",
    "based in the united states",
    "u.s. based",
    "us based",
    "u.s.-based",
    "us-based",
    "united states based",
    "u.s. only",
    "us only",
    "united states only",
    "no outsourcing outside the u.s.",
    "no outsourcing outside the us",
    "no outsourcing outside the united states",
    "us citizen",
    "u.s. citizen",
    "american citizen",
    # --- extensions for variants seen in the wild (incl. the exact phrase the user quoted) ---
    "only freelancers located in the u.s. may apply",
    "only freelancers located in the us may apply",
    "only freelancers located in the united states may apply",
    "located in the united states",
    "located in the us",
    "located in the u.s.",
    "remote \u2022 united states",
    "remote \u2022 usa",
    "remote \u2022 u.s.",
    "location: u.s.",
    "location: us",
    "location: united states",
    "us work authorization",
    "u.s. work authorization",
    "work authorization in the us",
    "work authorization in the u.s.",
    "must have us work authorization",
    "no offshore",
    "no outsourcing",
    "us residents",
    "u.s. residents",
    "us freelancers only",
    "u.s. freelancers only",
    "w-2",
    "w2 only",
    "in the united states only",
    "states only",
    "usa only",
    "u.s. only candidates",
    "us only candidates",
]


def has_us_location_restriction(block_text):
    """Return True if the listing block contains US-only location markers."""
    text_lower = block_text.lower()
    return any(marker in text_lower for marker in US_LOCATION_MARKERS)


# Detail-page markers — these appear on individual job pages (not search results)
# when scraped with includeTags=['section','div','span'] to force JS-rendered content extraction.
DETAIL_US_MARKERS = [
    "only freelancers located in the u.s. may apply",
    "only freelancers located in the us may apply",
    "u.s. located freelancers only",
    "us located freelancers only",
    "only freelancers located in the u.s.",
    "only freelancers located in the us",
    "only freelancers located in the united states",
    "only freelancers located in the united states may apply",
]


def check_job_detail_us_restriction(job_url):
    """Scrape job detail page with includeTags to extract JS-rendered location badge.
    Returns True if the job is US-only restricted."""
    payload = json.dumps({
        "url": job_url,
        "formats": ["markdown"],
        "timeout": 30000,
        "includeTags": ["section", "div", "span"],
    }).encode()
    req = Request(FIRECRAWL_URL, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=90) as resp:
            raw = resp.read()
        data = json.loads(raw)
        md = data.get("data", {}).get("markdown", "")
        if not md:
            return False
        md_lower = md.lower()
        for marker in DETAIL_US_MARKERS:
            if marker in md_lower:
                print(f"[FILTER-DETAIL] US-only restriction found on detail page: {job_url}", file=sys.stderr)
                return True
        return False
    except Exception as e:
        print(f"[WARN] Detail page check failed for {job_url}: {e}", file=sys.stderr)
        return False  # If we can't check, don't block the job


def clean_html(text):
    """Strip HTML tags and clean escaped backslashes."""
    text = re.sub(r'</?span[^>]*>', '', text)
    # Fix quadruple-escaped backslashes: \\\\\\\\ → \
    text = re.sub(r'\\\\\\\\+', '', text)
    # Collapse multiple consecutive backslashes
    text = re.sub(r'\\\\+', '', text)
    return text


def scrape_query(query, use_fallback=False):
    """Scrape and save to /tmp/upwork_{slug}.json, return markdown.
    Falls back to direct Firecrawl API if local proxy returns 5xx/connection error.
    """
    slug = re.sub(r'\W+', '_', query.lower())[:30]
    fpath = f"/tmp/upwork_{slug}.json"

    url = f"https://www.upwork.com/nx/search/jobs/?q={query.replace(' ', '+')}&sort=recency"
    payload = json.dumps({"url": url, "formats": ["markdown"], "timeout": 30000}).encode()

    endpoints = [
        (FIRECRAWL_URL, None),
    ]
    if use_fallback and FIRECRAWL_FALLBACK_KEY:
        endpoints.append((FIRECRAWL_FALLBACK_URL, FIRECRAWL_FALLBACK_KEY))

    last_error = None
    for endpoint, api_key in endpoints:
        req_headers = {"Content-Type": "application/json"}
        if api_key:
            req_headers["Authorization"] = f"Bearer {api_key}"

        req = Request(endpoint, data=payload, headers=req_headers)

        try:
            with urlopen(req, timeout=90) as resp:
                raw = resp.read()
                status = resp.status
            with open(fpath, 'wb') as f:
                f.write(raw)
            data = json.loads(raw)

            if not data.get("success") and "error" in data:
                err = data["error"]
                print(f"[WARN] Firecrawl returned error for '{query}' via {endpoint}: {err}", file=sys.stderr)
                last_error = RuntimeError(f"Firecrawl error: {err}")
                continue

            if status >= 400:
                print(f"[WARN] Firecrawl HTTP {status} for '{query}' via {endpoint}: {raw[:200]}", file=sys.stderr)
                last_error = RuntimeError(f"Firecrawl HTTP {status}")
                if status in (502, 503, 504) or status >= 500:
                    continue  # try fallback
                raise last_error

            return data.get("data", {}).get("markdown", "")
        except URLError as e:
            print(f"[WARN] Firecrawl connection failed for '{query}' via {endpoint}: {e}", file=sys.stderr)
            last_error = e
            continue
        except Exception as e:
            print(f"[WARN] Firecrawl failed for '{query}' via {endpoint}: {e}", file=sys.stderr)
            last_error = e
            continue

    if last_error:
        raise last_error
    raise RuntimeError("No Firecrawl endpoint succeeded")


def parse_jobs(markdown, query_label):
    """Parse Upwork job listings from markdown."""
    jobs = []
    # Split on "Posted " at line start
    blocks = re.split(r'\nPosted\s+', markdown)

    for block in blocks[1:]:  # skip sidebar
        job = {"query": query_label}

        # Time ago
        tm = re.match(r'(\d+\s+\w+\s+ago|yesterday|last\s+\w+)', block)
        job["posted"] = tm.group(1) if tm else "?"

        # Title + URL
        tm2 = re.search(
            r'##\s+\[(.+?)\]\((https://www\.upwork\.com/jobs/[^)]+)\)',
            block
        )
        if not tm2:
            continue

        # Skip US-only listings immediately (markdown-only heuristic; covers most cases)
        if has_us_location_restriction(block):
            print(f"[FILTER] Skipping US-only job: {clean_html(tm2.group(1))[:80]}...", file=sys.stderr)
            continue

        job["title"] = clean_html(tm2.group(1))
        # Strip query params from URL (clean URL for dedup)
        job["url"] = re.sub(r'\?.*$', '', tm2.group(2))
        # Clean HTML tags from URL slug
        job["url"] = re.sub(r'</?span[^>]*>', '', job["url"])
        job["url"] = re.sub(r'-(span-class-highlight-)+', '-', job["url"])

        # Job ID from URL
        jid_match = re.search(r'_~([0-9a-f]+)/?$', job["url"])
        job["id"] = jid_match.group(1) if jid_match else job["url"]

        # Budget
        hourly = re.search(r'\*\*Hourly:\s*\$([\d,.]+)\s*-\s*\$([\d,.]+)\*\*', block)
        fixed = re.search(r'\*\*Fixed price\*\*.*?\*\*Est\. budget:\*\*\s*\*\*\$([\d,.]+)\*\*', block, re.DOTALL)
        if hourly:
            job["rate"] = f"${hourly.group(1)}-{hourly.group(2)}/hr"
        elif fixed:
            job["rate"] = f"Fixed ${fixed.group(1)}"
        else:
            # Fallback
            alt_h = re.search(r'Hourly:\s*\$([\d,.]+)\s*-\s*\$([\d,.]+)', block)
            alt_f = re.search(r'Fixed price.*?budget:.*?\$([\d,.]+)', block, re.DOTALL)
            if alt_h:
                job["rate"] = f"${alt_h.group(1)}-{alt_h.group(2)}/hr"
            elif alt_f:
                job["rate"] = f"Fixed ${alt_f.group(1)}"
            else:
                job["rate"] = "—"

        # Experience
        exp = re.search(r'\*\*(Entry Level|Intermediate|Expert)\*\*', block)
        job["level"] = exp.group(1) if exp else "—"

        # Description — first meaningful paragraph (skip markdown headers, budget lines)
        desc = ""
        for line in block.split("\n"):
            line = line.strip()
            # Skip: empty, markdown formatting, skill tags, headers, budget lines
            if not line or len(line) < 60:
                continue
            if line.startswith("*") or line.startswith("##") or line.startswith("-") or line.startswith("#"):
                continue
            if re.match(r'^[\w\s&+\-/#,.]+$', line):
                continue
            if 'Est. time' in line or 'Est. budget' in line:
                continue
            desc = line[:250]
            break
        job["desc"] = clean_html(desc) if desc else "(no description)"

        # Relevance score
        text_lower = (job["title"] + " " + job["desc"]).lower()
        job["score"] = sum(1 for kw in KEYWORDS if kw in text_lower)

        jobs.append(job)

    return jobs


def load_seen():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE))
        except:
            pass
    return {}


def save_seen(seen):
    json.dump(seen, open(STATE_FILE, "w"), indent=2)


def parse_posted_time(posted_str, now=None):
    """Convert Upwork 'posted' string to datetime. Returns None if unparseable."""
    if now is None:
        now = datetime.now(timezone.utc)
    
    posted_str = posted_str.lower().strip()
    
    # "2 hours ago", "1 hour ago", "30 minutes ago"
    m = re.match(r'(\d+)\s+hours?\s+ago', posted_str)
    if m:
        return now - timedelta(hours=int(m.group(1)))
    
    m = re.match(r'(\d+)\s+minutes?\s+ago', posted_str)
    if m:
        return now - timedelta(minutes=int(m.group(1)))
    
    # "yesterday"
    if posted_str == 'yesterday':
        return now - timedelta(days=1)
    
    # "3 days ago", "1 day ago"
    m = re.match(r'(\d+)\s+days?\s+ago', posted_str)
    if m:
        return now - timedelta(days=int(m.group(1)))
    
    # "last week"
    if posted_str == 'last week':
        return now - timedelta(days=7)
    
    # "2 weeks ago"
    m = re.match(r'(\d+)\s+weeks?\s+ago', posted_str)
    if m:
        return now - timedelta(weeks=int(m.group(1)))
    
    # "last month", "1 month ago"
    if posted_str in ('last month', '1 month ago'):
        return now - timedelta(days=30)
    
    return None


def is_within_24h(posted_str, now=None):
    """Check if posted time is within the last 24 hours."""
    dt = parse_posted_time(posted_str, now)
    if dt is None:
        return False
    if now is None:
        now = datetime.now(timezone.utc)
    return (now - dt).total_seconds() <= 24 * 3600


def dedup_and_filter(jobs, seen):
    """Dedup only — no 24h filter, keep all unique jobs and persist state."""
    now = datetime.now(timezone.utc)
    new_seen = dict(seen)

    # Expire old entries after SEEN_EXPIRE_HOURS
    for jid, ts in list(new_seen.items()):
        try:
            age = (now - datetime.fromisoformat(ts)).total_seconds()
            if age > SEEN_EXPIRE_HOURS * 3600:
                del new_seen[jid]
        except:
            del new_seen[jid]

    new_jobs = []
    for job in jobs:
        if job["id"] not in new_seen:
            new_jobs.append(job)
            new_seen[job["id"]] = now.isoformat()

    return new_jobs, new_seen

def write_to_google_sheets(jobs):
    """Write new jobs to Google Sheets (Upwork tab). Same method as sales-event-vacancy-monitoring."""
    if not jobs:
        return 0

    SPREADSHEET_ID = "1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok"
    GID = 872962241  # Upwork tab

    creds_path = os.path.expanduser("~/.config/gws/credentials.json")
    if not os.path.exists(creds_path):
        print("[WARN] No Google Sheets credentials, skipping write", file=sys.stderr)
        return 0

    with open(creds_path) as fh:
        creds = json.load(fh)

    # 1. Get access token
    token_data = urllib.parse.urlencode({
        "client_id": creds["client_id"],
        "client_secret": creds["client_secret"],
        "refresh_token": creds["refresh_token"],
        "grant_type": "refresh_token",
    }).encode()
    req = urllib.request.Request("https://oauth2.googleapis.com/token", data=token_data)
    with urllib.request.urlopen(req) as resp:
        access_token = json.load(resp)["access_token"]

    # 2. Find sheet name by gid
    req = urllib.request.Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        sheet_info = json.load(resp)

    sheet_name = None
    for sheet in sheet_info["sheets"]:
        if sheet["properties"]["sheetId"] == GID:
            sheet_name = sheet["properties"]["title"]
            break

    if sheet_name is None:
        print(f"[ERROR] Sheet with gid {GID} not found", file=sys.stderr)
        return 0

    # 3. Get current row count
    from urllib.parse import quote
    sheet_encoded = quote(str(sheet_name), safe="")
    req = urllib.request.Request(
        f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{sheet_encoded}!A:E"
    )
    req.add_header("Authorization", f"Bearer {access_token}")
    with urllib.request.urlopen(req) as resp:
        values_data = json.load(resp)

    current_rows = len(values_data.get("values", []))
    next_row = current_rows + 1

    # 4. Build rows: title, price, level, published, description
    today = datetime.now().strftime("%d.%m.%Y")
    rows = []
    for j in jobs:
        rows.append([
            today,          # A: Date
            j["title"],     # B: Title
            j["rate"],      # C: Price
            j["level"],     # D: Level
            j["posted"],    # E: Published
            j["desc"],      # F: Description
            j["url"],       # G: URL
        ])

    end_row = next_row + len(rows) - 1
    range_str = f"{sheet_encoded}!A{next_row}:G{end_row}"

    body = json.dumps({"values": rows}).encode()
    url = f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}/values/{range_str}?valueInputOption=RAW"
    req = urllib.request.Request(url, data=body, method="PUT")
    req.add_header("Authorization", f"Bearer {access_token}")
    req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req) as resp:
            result = json.load(resp)
        written = result.get("updatedRows", 0)
        print(f"[Sheets] Written {written} rows to '{sheet_name}'", file=sys.stderr)
        return written
    except Exception as e:
        print(f"[Sheets ERROR] {e}", file=sys.stderr)
        return 0
SHEET_URL = "https://docs.google.com/spreadsheets/d/1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok/edit#gid=872962241"


def get_total_rows(access_token):
    """Get total number of data rows in the Upwork sheet (excluding header).

    Retries on transient HTTP 5xx: a single Google Sheets API glitch must not
    fail the whole daily report (same policy as kwork_check.py fetch retry).
    """
    SPREADSHEET_ID = "1R4uQG-yy2mZ4zuJVkQgrfxoVW6N60suUmnEVKBFolok"
    GID = 872962241

    for attempt in range(3):
        try:
            # Find sheet name by gid
            req = Request(f"https://sheets.googleapis.com/v4/spreadsheets/{SPREADSHEET_ID}")
            req.add_header("Authorization", f"Bearer {access_token}")
            with urlopen(req, timeout=30) as resp:
                sheet_info = json.load(resp)
            break
        except HTTPError as e:
            if e.code >= 500 and attempt < 2:
                print(f"[Sheets] HTTP {e.code} on get_total_rows (attempt {attempt + 1}), retrying in 15s...", file=sys.stderr)
                time.sleep(15)
            else:
                print(f"[Sheets] get_total_rows failed after {attempt + 1} attempt(s): {e}", file=sys.stderr)
                return 0

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
    # If first row is header "Date", subtract 1
    if total_rows > 0 and rows[0] and rows[0][0].lower() == "date":
        total_rows -= 1
    return total_rows


def get_access_token():
    """Get Google Sheets access token from credentials."""
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


def categorize_job(title):
    """Assign a job to a category based on its title keywords."""
    t = title.lower()
    if any(k in t for k in ["n8n", "zapier", "make", "workflow", "automation", "agent", "bot", "hyperagent", "claude", "langchain", "langgraph", "llm", "ai agent", "aiagent", "ai automation", "ai workflow", "ai engineer", "ai developer", "ai architect", "ai/ml", "ai/llm", "ai-first", "ai native", "ai-native"]):
        return "🤖 AI-агенты / автоматизация"
    if any(k in t for k in ["full-stack", "full stack", "fullstack", "react", "node", "python", "flutter", "front", "backend", "web", "saas", "mvp", "api", "developer", "engineer", "dev"]):
        return "🛠 Full-Stack / AI-разработка"
    if any(k in t for k in ["crm", "data", "database", "hubspot", "gohighlevel", "mls", "idx", "integration", "sheets", "excel", "lead", "marketing", "paid media", "seo"]):
        return "📊 Данные / CRM / интеграции"
    return "🔬 Прочее"


def format_telegram(jobs, total_rows, max_jobs=5):
    """Format Telegram report: date header, stats, jobs grouped by category."""
    today_str = datetime.now().strftime("%d.%m.%Y")

    if not jobs:
        lines = [
            f"*Upwork: {today_str}*",
            "",
            f"Новых заданий: 0 | Всего в таблице: {total_rows}",
            "",
            f"[🔗 Открыть таблицу]({SHEET_URL})",
        ]
        return "\n".join(lines)

    lines = [
        f"*Upwork: {today_str}*",
        "",
        f"Новых заданий: {len(jobs)} | Всего в таблице: {total_rows}",
        "",
        f"[🔗 Открыть таблицу]({SHEET_URL})",
        "",
        "=====",
    ]

    # Group by category, preserving original order within each group
    categories = ["🔥 Топ по ставке", "🤖 AI-агенты / автоматизация",
                  "🛠 Full-Stack / AI-разработка", "📊 Данные / CRM / интеграции", "🔬 Прочее"]
    grouped = {c: [] for c in categories}
    for j in jobs:
        grouped[categorize_job(j["title"])].append(j)

    # Top by rate: parse hourly max rate, sort desc
    def max_rate(j):
        m = re.search(r"\$([\d,.]+)-([\d,.]+)/hr", j["rate"])
        if m:
            return float(m.group(2).replace(",", ""))
        m = re.search(r"Fixed \$([\d,.]+)", j["rate"])
        if m:
            return float(m.group(1).replace(",", ""))
        return 0.0

    top = sorted(jobs, key=max_rate, reverse=True)[:3]
    grouped["🔥 Топ по ставке"] = top

    for cat in categories:
        items = grouped[cat]
        if not items:
            continue
        lines.append("")
        lines.append(f"*{cat}*")
        for j in items:
            title = j['title'].rstrip(':').strip()
            safe_title = title.replace(']', ' ').replace('[', ' ').replace('\\', ' ')
            lines.append(f"- [{safe_title}]({j['url']}) · {j['rate']}")

    return "\n".join(lines)


def send_telegram(text, parse_mode="HTML", reply_markup=None):
    """Send message to all chats via multicast helper."""
    return tg_multicast.send_multicast(
        TG_BOT_TOKEN, TG_CHAT_IDS, text,
        parse_mode=parse_mode, reply_markup=reply_markup, tag="Upwork",
    )


def cover_letter_keyboard(order_id: str) -> dict:
    """Inline keyboard with 'Написать отклик' button."""
    return {
        "inline_keyboard": [[
            {"text": "📝 Написать отклик", "callback_data": f"reply:upwork:{order_id}"}
        ]]
    }


def append_jsonl(obj: dict) -> None:
    """Append order to JSONL cache for cover letter bot lookup."""
    with open(OUT_JSONL, "a", encoding="utf-8") as f:
        f.write(json.dumps(obj, ensure_ascii=False) + "\n")


def main():
    print("[Upwork Scraper] Starting...", file=sys.stderr)

    all_jobs = []
    firecrawl_error = None
    proxy_failed = False
    for q in QUERIES:
        try:
            md = scrape_query(q, use_fallback=proxy_failed)
        except Exception as e:
            firecrawl_error = str(e)
            break
        if not md and not proxy_failed:
            # Empty markdown from proxy — try fallback once for remaining queries
            proxy_failed = True
            try:
                md = scrape_query(q, use_fallback=True)
            except Exception as e:
                firecrawl_error = str(e)
                break
        if md:
            jobs = parse_jobs(md, q)
            print(f"[Upwork Scraper] Query '{q[:30]}': {len(jobs)} jobs", file=sys.stderr)
            all_jobs.extend(jobs)
        else:
            # Truly empty response — treat as error if fallback also empty
            firecrawl_error = "Empty markdown from both proxy and fallback"
            break

    # If Firecrawl failed on any query, report the error instead of fake zero
    if firecrawl_error:
        print(f"[Upwork Scraper] Aborted due to Firecrawl error: {firecrawl_error}", file=sys.stderr)
        today_str = datetime.now().strftime("%d.%m.%Y")
        error_report = (
            f"*Upwork: {today_str}*\n\n"
            "⚠️ Скрапер не смог получить данные с Upwork.\n"
            f"Причина: `{firecrawl_error}`\n\n"
            "Проверь Firecrawl credits / proxy."
        )
        print(error_report)
        return

    # Within-run dedup by URL
    seen_urls = set()
    unique = []
    for j in all_jobs:
        if j["url"] not in seen_urls:
            seen_urls.add(j["url"])
            unique.append(j)
    print(f"[Upwork Scraper] After within-run dedup: {len(unique)} unique", file=sys.stderr)

    # Cross-run dedup
    seen = load_seen()
    new_jobs, updated_seen = dedup_and_filter(unique, seen)

    new_count = len(updated_seen) - len(seen)
    print(f"[Upwork Scraper] New jobs: {len(new_jobs)}, newly tracked: {new_count}", file=sys.stderr)

    # Sort by relevance
    new_jobs.sort(key=lambda j: (-j["score"]))

    # === US-only filter via detail page scrape ===
    # Search results don't show location restrictions; the "Only freelancers located
    # in the U.S. may apply" badge is JS-rendered and only visible on the job detail page.
    # Scrape each new job's detail page (with includeTags to force badge extraction) and
    # filter out US-only restricted jobs before they reach Google Sheets / Telegram.
    if new_jobs:
        print(f"[Upwork Scraper] Checking {len(new_jobs)} new jobs for US-only restriction via detail pages (parallel)...", file=sys.stderr)
        from concurrent.futures import ThreadPoolExecutor, as_completed
        worldwide_jobs = []
        us_blocked = []
        # Parallel detail-page checks: 5 workers keeps total time under ~60s for 24 jobs
        with ThreadPoolExecutor(max_workers=5) as pool:
            future_map = {pool.submit(check_job_detail_us_restriction, j["url"]): j for j in new_jobs}
            for future in as_completed(future_map):
                j = future_map[future]
                try:
                    is_us = future.result()
                except Exception:
                    is_us = False
                if is_us:
                    us_blocked.append(j)
                else:
                    worldwide_jobs.append(j)
        if us_blocked:
            print(f"[Upwork Scraper] Filtered out {len(us_blocked)} US-only jobs:", file=sys.stderr)
            for j in us_blocked:
                print(f"  ❌ US-only: {j['title'][:70]}", file=sys.stderr)
        new_jobs = worldwide_jobs
        print(f"[Upwork Scraper] After US-only detail filter: {len(new_jobs)} worldwide jobs", file=sys.stderr)

    # Save state (after filter, so US-only jobs are still marked as seen → not re-checked)
    save_seen(updated_seen)

    # Get total rows from Google Sheets
    total_rows = 0
    token = get_access_token()
    if token:
        total_rows = get_total_rows(token)

    # Write to Google Sheets
    write_to_google_sheets(new_jobs)

    # Stats for debug
    for j in new_jobs[:3]:
        print(f"  - [{j['score']}] {j['title'][:60]}...", file=sys.stderr)

    # Send header (stats summary) — no inline button
    header = format_telegram(new_jobs, total_rows)
    print(header)  # keep stdout for cron agent compatibility
    send_telegram(header, parse_mode="Markdown")

    # Send each job individually with inline "Написать отклик" button
    if new_jobs:
        time.sleep(1)
        for j in new_jobs[:5]:  # top 5 only (same as format_telegram max_jobs)
            job_id = j.get("id", j.get("url", ""))
            title = j['title'].rstrip(':').strip()
            msg = (
                f"#Upwork\n\n<b>{html.escape(title)}</b>\n"
                f"💰 {j['rate']}  |  🎯 {j['level']} |  🕐 {j['posted']}\n"
            )
            if j["desc"] != "(no description)":
                desc_short = j["desc"][:300]
                if len(j["desc"]) > 300:
                    desc_short += "…"
                msg += f"\n📝 {html.escape(desc_short)}\n"
            msg += f"\n🔗 <a href=\"{j['url']}\">{job_id}</a>"
            send_telegram(msg, reply_markup=cover_letter_keyboard(job_id))
            time.sleep(0.5)

    # Save to JSONL for cover letter bot
    for j in new_jobs:
        append_jsonl({
            "id": j.get("id", j.get("url", "")),
            "title": j.get("title", ""),
            "description": j.get("desc", ""),
            "price": j.get("rate", ""),
            "url": j.get("url", ""),
            "source": "upwork",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        })


if __name__ == "__main__":
    main()
