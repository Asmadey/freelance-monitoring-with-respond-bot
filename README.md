# Freelance Monitoring with Respond Bot

Автоматизированная система мониторинга 6 фриланс-бирж с генерацией персонализированных откликов на заказы через LLM по нажатию одной кнопки в Telegram.

## Что делает

### 1. Мониторинг бирж (6 источников)

Скраперы регулярно проверяют новые заказы на 6 фриланс-площадках и присылают их в Telegram-чат в виде структурированных сообщений с inline-кнопкой **«Написать отклик»**.

| Источник | Файл | Метод | Расписание |
|---|---|---|---|
| Profi.ru | `profi_graphql_check.py` | GraphQL API (warp/v2) | каждые 5 мин |
| YouDo | `youdo_check.py` | REST API + HMAC + Firecrawl | каждые 5 мин |
| Kwork | `kwork_check.py` | RSS + API | каждые 5 мин |
| FL.ru | `fl_ru_scraper.py` | HTML scraping | каждые 5 мин |
| Freelance.ru | `freelance_ru_scraper.py` | HTML scraping | каждые 5 мин |
| Upwork | `upwork_scraper.py` | Firecrawl + parallel US-only filter | ежедневно 07:00 MSK |

Каждый скрапер:
- Авторизуется / обходит защиту (VPN, cookies, headers, HMAC подписи)
- Извлекает новые заказы с полным описанием
- Записывает в JSONL-файл (для последующего lookup при генерации отклика)
- Отправляет в Telegram-чат карточку заказа с inline-кнопкой через `tg_multicast.py`

### 2. Генерация откликов (Cover Letter Bot)

`cover_letter_bot.py` — FastAPI-сервис, принимающий webhook от Telegram при нажатии кнопки **«Написать отклик»**.

**Пайплайн:**

```
Пользователь нажимает «Написать отклик»
        ↓
Telegram → webhook → FastAPI
        ↓
callback_data: reply:{source}:{order_id}
        ↓
JSONL lookup → полный текст заказа
        ↓
LLM (deepseek-v4-flash) + system prompt
        ↓
Отклик (copy-pasteable, <pre> форматирование)
        ↓
Telegram-ответ
```

**LLM:** deepseek-v4-flash-0731 через Ollama Cloud API (OpenRouter). System prompt задаёт структуру отклика:
1. Приветствие с именем клиента (извлекается из текста заказа)
2. Кто я — один абзац (фиксированный: 3 года внедрения ИИ, 15+ решений, proshinsky.com)
3. Польза для клиента — адаптируется под конкретное задание
4. Короткое описание предполагаемого решения
5. Призыв к действию в виде вопроса

**Два режима system prompt:**
- **Default** — для фриланс-заказов (AI-агенты, автоматизация, скрапинг)
- **Sales & Event** — для вакансий Sales Manager / Event Manager (кандидат: Анна Калягина)

**YouDo fallback:** Прямой `requests.get()` к youdo.com даёт 403 (WAF). Для youdo используется Firecrawl — on-demand scraping `https://youdo.com/t{id}` с извлечением секции «Нужно».

### 3. Инфраструктура

| Компонент | Технология | Назначение |
|---|---|---|
| Telegram-бот | @menti\_progress\_bot | Доставка заказов + приём callback |
| Webhook-сервер | FastAPI (Python) | Обработка нажатий кнопок |
| Reverse proxy | Caddy | TLS-терминация + Let's Encrypt |
| Process manager | systemd | Автозапуск + restart on failure |
| LLM | deepseek-v4-flash-0731 (Ollama Cloud) | Генерация откликов |
| Web scraping | Firecrawl (local proxy + rotator) | Fallback для youdo.com, Upwork |
| VPN | Xray (SOCKS5 :10808) | Доступ к api.profi.ru |
| Хранилище | JSONL files | Lookup заказов по ID |
| Multicast | `tg_multicast.py` | Отправка карточек всем авторизованным пользователям |

## Польза для потребителя

### До
- Ручной мониторинг 6 бирж — **2–3 часа в день**
- Написание откликов вручную — **5–10 минут на каждый**
- Опоздание на новые заказы — конкуренты быстрее
- Отклики шаблонные, не адаптированы под заказ

### После
- Новые заказы приходят в Telegram автоматически — **0 минут на мониторинг**
- Отклик генерируется за **3 секунды** по нажатию кнопки
- Отклик персонализирован: имя клиента, польза под конкретную задачу, предложение решения
- Время от первого появления заказа до отправки отклика — **менее 5 минут**
- Можно откликнуться на 10 заказов за 2 минуты

### Конкретные метрики

| Метрика | До | После |
|---|---|---|
| Мониторинг бирж | 2–3 ч/день | 0 (автоматически) |
| Время на отклик | 5–10 мин/шт | 3 сек/шт |
| Покрытие бирж | 1–2 вручную | 6 автоматически |
| Качество отклика | Шаблонный | Персонализированный |
| Скорость реакции | Часы | Минуты |

## Установка

### Требования

- Python 3.12+
- Linux-сервер с systemd
- Caddy (reverse proxy с автоматическим TLS)
- Telegram-бот (токен от @BotFather)
- Ollama Cloud API ключ (или OpenRouter)
- Firecrawl (локальный прокси на :9123 + ротатор ключей)
- Xray VPN (для profi.ru)

### Шаги

```bash
# 1. Клонировать репозиторий
git clone https://github.com/Asmadey/freelance-monitoring-with-respond-bot.git
cd freelance-monitoring-with-respond-bot

# 2. Установить зависимости
pip install fastapi uvicorn httpx requests python-dotenv

# 3. Создать токен-файл для бота
echo "YOUR_BOT_TOKEN" > fl_aibot_token.txt

# 4. Создать chat IDs файл (пользователи, нажавшие /start)
cat > fl-aibot-chats.json << 'EOF'
[128204572]
EOF

# 5. Создать .env
cat > .env << 'EOF'
OLLAMA_API_KEY=your_ollama_key
FIRECRAWL_API_KEY=your_firecrawl_key
OPENROUTER_BASE_URL=https://openrouter.ai/api/v1
EOF

# 6. Создать systemd service
sudo cp cover-letter-bot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now cover-letter-bot

# 7. Настроить Caddy reverse proxy
# cover.your-domain.com → localhost:9876

# 8. Установить webhook
curl "https://api.telegram.org/bot<TOKEN>/setWebhook?url=https://cover.your-domain.com/webhook&allowed_updates=%5B%22callback_query%22%5D"

# 9. Настроить cron для скраперов (или использовать Hermes cron)
# */5 * * * * /usr/bin/python3 /path/to/profi_graphql_check.py
# */5 * * * * /usr/bin/python3 /path/to/youdo_check.py
# 0 7 * * *   /usr/bin/python3 /path/to/upwork_scraper.py
# и т.д.
```

## Структура проекта

```
freelance-monitoring-with-respond-bot/
├── cover_letter_bot.py              # FastAPI webhook + LLM генерация
├── cover_letter_system_prompt.md    # System prompt для LLM (default)
├── tg_multicast.py                  # Shared helper: multicast карточки + chat ID management
├── profi_graphql_check.py           # Profi.ru GraphQL scraper + client name fetch
├── youdo_check.py                   # YouDo scraper (multi-query + HMAC + Firecrawl)
├── kwork_check.py                   # Kwork scraper
├── fl_ru_scraper.py                 # FL.ru scraper
├── freelance_ru_scraper.py          # Freelance.ru scraper
├── upwork_scraper.py                # Upwork scraper (Firecrawl + parallel US-only filter)
├── freelance_ai_daily_report.py     # Aggregated daily report (all 3 RU sources → single TG message)
├── .gitignore
└── README.md
```

## Технические детали

### Telegram-бот

Система использует одного бота (@menti\_progress\_bot) для всех 6 скраперов и cover letter bot. Токен хранится в `fl_aibot_token.txt` (единоe хранилище для всех скриптов).

**tg\_multicast.py** — shared helper:
- `get_chat_ids()` — загружает список авторизованных chat IDs из `fl-aibot-chats.json`
- `send_multicast()` — отправляет сообщение всем chat IDs, автоматически удаляет заблокировавшие (403)
- Все скраперы и cover_letter_bot используют этот helper

**Важно:** пользователь должен нажать `/start` в боте — Telegram запрещает ботам инициировать переписку первым (403 Forbidden).

### Callback data формат

```
reply:{source}:{order_id}
```

- `source` — `profi` | `youdo` | `kwork` | `fl` | `freelance` | `upwork`
- `order_id` — ID заказа на бирже
- Максимум 64 байта (ограничение Telegram)

### JSONL lookup

Каждый скрапер записывает заказы в JSONL-файл:
```json
{"id": "92129112", "title": "AI-агент для продаж", "description": "...", "url": "...", "source": "profi", "collected_at": "2026-08-27T09:18:28Z"}
```

При нажатии кнопки бот читает JSONL-файл соответствующего источника, находит заказ по `order_id` и передаёт полный текст в LLM.

### LLM вызов

```python
POST {OPENROUTER_BASE_URL}/chat/completions
{
  "model": "deepseek/deepseek-v4-flash-0731",
  "messages": [
    {"role": "system", "content": "<system_prompt>"},
    {"role": "user", "content": "<order_text>"}
  ],
  "max_tokens": 4096,
  "temperature": 0.7
}
```

### Per-source cover letter limits

| Source | Max chars |
|---|---|
| profi.ru | 500 (platform cap) |
| upwork | 1200 |
| kwork | 1200 |
| fl.ru | 1200 |
| freelance.ru | 1200 |
| youdo | 1200 |

### Upwork: US-only фильтр

Upwork-скрапер фильтрует вакансии, доступные только для US-фрилансеров. Проверка двухуровневая:

1. **Listing-level:** поиск маркеров в markdown результатов (`"us only"`, `"based in u.s."` и т.д.)
2. **Detail-page-level:** badge «Only freelancers located in the U.S. may apply» JS-рендерится и виден только на странице вакансии. Скрапер проверяет каждую detail page через Firecrawl с `includeTags: ["section", "div", "span"]`

**Параллелизация:** detail-page проверки выполняются через `ThreadPoolExecutor(max_workers=5)` — 24 вакансии проверяются за ~60с вместо ~600с при последовательном выполнении.

### VPN для Profi.ru

API profi.ru (`api.profi.ru`) доступен только через российский IP. Скрапер использует SOCKS5-прокси через Xray:
- `socks5h://127.0.0.1:10808` — DNS резолвится через прокси (не локально)
- Xray-сервис: `xray-profi.service` (systemd)
- Auto-fallback: при недоступности SOCKS5 → прямой connection
- App version: `x-warp-ui-ver: 1.152.2`, `User-Agent: rbo/261981023`

### YouDo multi-query search

Youdo API использует HMAC-SHA256 подписи (per-URL, `KeyVersion: 1`). Подпись вычисляется над точным URL — смена любого параметра ломает подпись. Подписи стабильны — не протухают.

Скрапер запрашивает 6 endpoints:
1. General IT listing (Category=4194304, без поиска, 50 tasks)
2. `q=ии` (все категории Cat=1, 26 tasks)
3. `q=AI` (Cat=4194304, 3 tasks)
4. `q=Ии` (Cat=4194304, 3 tasks)
5. `q=aeo` (Cat=1, 1 task)
6. `q=AEO` (Cat=4194304, 1 task)

Дедупликация по task ID.

### YouDo description + WAF bypass

Листинг API возвращает `Description: ""`. Полное описание скрапится через Firecrawl:
- `POST http://127.0.0.1:9123/v2/scrape` с URL `https://youdo.com/t{id}`
- Извлекается секция «Нужно» (полное описание задачи)
- `servicepipe.tech` JS-challenge блокирует raw `requests.get()` — Firecrawl обходит через headless render

### Profi.ru client name fetch

GraphQL `BoSearchBoardItems` возвращает `clientInfo.name: null`. Имя клиента fetch'ится через mobile backoffice API:
- `POST https://api.profi.ru/mobile/backoffice/v2/` (multipart/form-data)
- Method: `getKlientInfo`, `data.order_id`
- Response: `data.name` (например «Леонид»)

### Firecrawl ротатор

Локальный Firecrawl-прокси на `127.0.0.1:9123` ротирует между 5 API-ключами для обхода rate limits. Watchdog (`firecrawl_rotator_watchdog.sh`) каждую минуту проверяет здоровье прокси и перезапускает при необходимости.

## Автор

**Влад** — 3 года внедряет ИИ в бизнес: AI-агенты, RAG-боты, голосовые агенты, LLM-автоматизации. 15+ запущенных решений.

- Сайт: [proshinsky.com](https://proshinsky.com)
- Telegram: [@asmadeyi](https://t.me/asmadeyi)