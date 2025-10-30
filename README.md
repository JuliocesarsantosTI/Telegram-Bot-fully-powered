# Dobby Chat – Telegram Bot README

A lightweight Telegram bot that forwards user messages to a configurable backend “executions” API, polls for completion, and returns the final text response. Built with `python-telegram-bot` (v20+) and `requests`.

---

## Features

- 🔌 **Pluggable backend**: point the bot to any API exposing `/api/v1/executions`.
- 🔁 **Async-style polling**: POST to create an execution, poll `/status`, then GET the final result.
- 🧰 **Admin commands**: `/seturl`, `/setheaders`, `/show`, `/raw`, `/ping`, plus `/start`, `/help`, `/privacy`.
- ⚠️ **Resilient networking**: retry, timeouts, and trimmed long messages.
- 🧾 **Structured logging**: simple, informative log lines.

---

## Architecture (high level)

```
Telegram User  ──>  Bot (this repo)  ──POST /executions──>  Backend
       ▲                   │                   │
       │               Poll status             ▼
       └────── reply <── GET /executions/{id} ◄── final_result
```

**Backend contract expected:**

1) `POST {API_BASE}/executions`
   - Body example: `{"goal": "Hello", "user_id": 123, "max_depth": 1, ...}`
   - Returns JSON containing `execution_id` (HTTP 2xx).

2) `GET  {API_BASE}/executions/{execution_id}/status`
   - Returns JSON with `status` (e.g., `running`, `completed`, `failed`, `timeout`, `cancelled`).

3) `GET  {API_BASE}/executions/{execution_id}`
   - Returns JSON with `final_result`, which may be:
     - a string: `"final text…"`
     - or an object containing one of: `result`, `final`, `text`, `message`, `output` (string).

4) `GET  {API_BASE}/health` (for `/ping`).

---

## Requirements

- **Python**: 3.10+
- **Telegram bot token** from [@BotFather](https://t.me/BotFather)
- **Dependencies**:
  - `python-telegram-bot>=20`
  - `requests`
  - `urllib3` (bundled via `requests`)

Create a `requirements.txt`:

```
python-telegram-bot>=20,<21
requests>=2.31.0
urllib3>=2.0.0
```

Install:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

---

## Configuration

Environment variables:

- `TELEGRAM_BOT_TOKEN` *(required)* – token from BotFather.
- Backend defaults (overridable at runtime via commands):
  - `API_URL_DEFAULT` → `http://localhost:8000/api/v1/executions`
  - `API_BASE_DEFAULT` → derived from `API_URL_DEFAULT` (path without `/executions`)

Optional runtime knobs (tweak in code or via custom logic):
- `max_user_msg_len` (default **2000**)
- `poll_interval` seconds (default **1.0**)
- `max_wait` seconds (default **120.0** for status loop)

Example `.env`:

```env
TELEGRAM_BOT_TOKEN=123456:ABC-DEF-your-telegram-token
```

Export and run:

```bash
export $(grep -v '^#' .env | xargs)
python bot.py
```

---

## Running

```bash
python bot.py
```

You should see:

```
INFO | tg-bot | Bot is running with long polling…
```

The bot uses **long polling** (`app.run_polling(...)`). Webhooks are not configured in this code.

---

## Bot Commands (for admins and users)

- `/start` – Welcome text.
- `/help` – How to use the bot.
- `/privacy` – Minimal privacy statement.
- `/show` – Show current API URL/base and headers in use.
- `/seturl <url>` – Set the executions endpoint.  
  Example:
  ```
  /seturl http://localhost:8000/api/v1/executions
  ```
- `/setheaders <json>` – Replace HTTP headers (useful for auth).  
  Example:
  ```
  /setheaders {"Authorization":"Bearer YOUR_TOKEN","Content-Type":"application/json","Accept":"application/json"}
  ```
  > Note: `GET` calls automatically drop `Content-Type`.
- `/raw <json>` – Send a raw JSON body to `POST /executions`, show raw response (status + JSON/text).  
  Example:
  ```
  /raw {"goal":"hello world","max_depth":1}
  ```
- `/ping` – GET `{api_base}/health` and show the HTTP status.

**Default behavior (plain text message):**  
Any non-command text is treated as a **goal** for the backend. The bot:
1. `POST /executions` with `{goal, user_id, max_depth=1, ...}`
2. Polls `/executions/{id}/status` until terminal (`completed/failed/timeout/cancelled`)
3. `GET /executions/{id}`
4. Extracts text from `final_result` and replies.

If the message exceeds `max_user_msg_len`, it is trimmed and the user is warned.

---

## Backend Examples

Quick `curl` stubs for a compatible backend:

```bash
# 1) Create an execution
curl -X POST http://localhost:8000/api/v1/executions   -H "Content-Type: application/json" -d '{
    "goal":"Say hi",
    "user_id": 42,
    "max_depth": 1
  }'
# => {"execution_id":"abc123"}

# 2) Status
curl http://localhost:8000/api/v1/executions/abc123/status
# => {"status":"completed"}

# 3) Result
curl http://localhost:8000/api/v1/executions/abc123
# => {"status":"completed","final_result":{"result":"Hello there!"}}
```

The bot accepts both a string `final_result` or an object containing any of:
`result | final | text | message | output`.

---

## Deployment

### Docker (example)

`Dockerfile`:

```dockerfile
FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .  # rename your file accordingly
ENV PYTHONUNBUFFERED=1
CMD ["python", "bot.py"]
```

Build & run:

```bash
docker build -t dobby-chat-bot .
docker run --rm -e TELEGRAM_BOT_TOKEN=$TELEGRAM_BOT_TOKEN dobby-chat-bot
```

### Systemd (optional)

`/etc/systemd/system/dobby-bot.service`:

```
[Unit]
Description=Dobby Chat Telegram Bot
After=network.target

[Service]
Type=simple
WorkingDirectory=/opt/dobby-bot
Environment=TELEGRAM_BOT_TOKEN=123456:ABC...
ExecStart=/opt/dobby-bot/.venv/bin/python bot.py
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now dobby-bot
```

---

#

## Troubleshooting

- **`Please set TELEGRAM_BOT_TOKEN`**  
  Set the env var or edit code to provide a token.

- **No response from backend**  
  - Verify `/ping` → `{api_base}/health` is reachable.
  - Check `/show` to confirm URL/headers.
  - Inspect backend logs for `POST /executions`.

- **`network_err` or `timeout`**  
  - Backend is down or slow; increase `max_wait` or `poll_interval` if needed.
  - Check proxies/firewall.

- **Final result is “Success, but no simple text field found”**  
  - Your backend returned `final_result` without a recognized string field.
  - Ensure `final_result` is a string **or** an object with one of:
    `result|final|text|message|output` (string).

---

## Code Map

- `main()` – boots the bot, registers handlers, starts long polling.
- `handle_text()` – trims user text, calls `handle_default_route()`.
- `handle_default_route()` – POST → poll `/status` → GET result → extract text.
- `post_json()`, `_get_status()`, `_get_result()` – HTTP helpers.
- `_extract_final_text_from_payload()` – robust final text extraction.
- `build_session()` – `requests.Session` with retries and shared pool.
- Command handlers:
  - `start`, `help_cmd`, `privacy`, `show`, `seturl`, `setheaders`, `raw`, `ping`, `unknown`.

---



## Quick Start (TL;DR)

```bash
# 1) Install
python3 -m venv .venv && source .venv/bin/activate
pip install python-telegram-bot requests

# 2) Configure
export TELEGRAM_BOT_TOKEN=123456:ABC-DEF...

# 3) Run
python bot.py

# 4) In Telegram
/start
/seturl http://localhost:8000/api/v1/executions
/setheaders {"Authorization":"Bearer <token>","Content-Type":"application/json","Accept":"application/json"}
hello 👋
```
