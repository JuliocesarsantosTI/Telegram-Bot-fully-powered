import os
import json
import logging
import time
from typing import Optional, Any, Dict, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import ApplicationBuilder, CommandHandler, MessageHandler, ContextTypes, filters


TELEGRAM_BOT_TOKEN = os.getenv(
    "TELEGRAM_BOT_TOKEN",
    "YOUR API KEY"  
)

API_URL_DEFAULT = "http://localhost:8000/api/v1/executions"  
API_BASE_DEFAULT = API_URL_DEFAULT.rsplit("/executions", 1)[0]  


RUNTIME: Dict[str, Any] = {
    "api_url": API_URL_DEFAULT,      
    "api_base": API_BASE_DEFAULT,      
    "headers": {"Content-Type": "application/json", "Accept": "application/json"},
    "max_user_msg_len": 2000,
    "poll_interval": 1.0,              
    "max_wait": 120.0,                 
    "session": None,                 
}


logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("tg-bot")


UX: Dict[str, str] = {

    "start": (
        "Hi welcome to Dobby chat\n\n"
        "Dobby chat fully powered by Roma sentientAGI\n"
    ),

   
    "help": "Send me any message and I’ll ask the backend, then return the final result.",
    "privacy": "I don’t store data. I only forward your text to the configured backend.",
    "show": "Current settings:\nURL: {url}\nBase: {base}\nHeaders: {headers}",
    "seturl_usage": "Usage: /seturl http://host:port/api/v1/executions",
    "seturl_ok": "✅ API URL set:\n{url}\n(base: {base})",
    "setheaders_usage": 'Usage: /setheaders {"Authorization":"Bearer ..."}',
    "setheaders_ok": "✅ Headers updated:\n{headers}",
    "setheaders_err": "❌ Invalid JSON headers.",
    "raw_usage": 'Usage: /raw {"goal":"hello","max_depth":1}',
    "timeout": "⚠️ Backend timed out. Try again shortly.",
    "network_err": "❌ Network error. Backend unreachable.",
    "backend_err": "❌ Backend error {code}:\n{body}",
    "success_no_text": "✅ Success, but no simple text field found:\n{body}",
    "unknown_cmd": "Unknown command. Type anything to run the pipeline.",
    "trim_warn": "Note: your message was long; I sent the first {n} characters.",
    "ping_ok": "✅ Ping OK: GET {url}\nStatus: {code}",
    "ping_err": "❌ Ping failed: GET {url}\n{details}",
}


def build_session() -> requests.Session:
    """requests.Session with small retries. Respects HTTP(S)_PROXY from env."""
    session = requests.Session()
    retry = Retry(
        total=2,
        connect=2,
        read=2,
        backoff_factor=0.3,
        status_forcelist=(502, 503, 504),
        allowed_methods=frozenset(["GET", "POST"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session

def _headers_no_ct() -> Dict[str, str]:
    """Return headers without Content-Type (for GETs)."""
    return {k: v for k, v in RUNTIME["headers"].items() if k.lower() != "content-type"}

def _json_compact(obj: Any, limit: int = 2000) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False, indent=2)
    except Exception:
        s = str(obj)
    return s if len(s) <= limit else s[:limit] + " …"

def _soft_trim_user_text(s: str) -> Tuple[str, Optional[str]]:
    limit = int(RUNTIME.get("max_user_msg_len", 2000))
    if len(s) > limit:
        return s[:limit], UX["trim_warn"].format(n=limit)
    return s, None

def post_json(body: Dict[str, Any], timeout: int = 30) -> requests.Response:
    session: requests.Session = RUNTIME["session"]
    return session.post(RUNTIME["api_url"], json=body, headers=RUNTIME["headers"], timeout=timeout)

def _get_status(execution_id: str) -> Dict[str, Any]:
    session: requests.Session = RUNTIME["session"]
    url = f"{RUNTIME['api_base']}/executions/{execution_id}/status"
    r = session.get(url, headers=_headers_no_ct(), timeout=10)
    r.raise_for_status()
    return r.json()

def _get_result(execution_id: str) -> Dict[str, Any]:
    session: requests.Session = RUNTIME["session"]
    url = f"{RUNTIME['api_base']}/executions/{execution_id}"
    r = session.get(url, headers=_headers_no_ct(), timeout=15)
    r.raise_for_status()
    return r.json()

def _extract_final_text_from_payload(payload: Dict[str, Any]) -> Optional[str]:
    """
    Shapes:
      {"status":"completed","final_result":{"result":"...","status":"COMPLETED",...}}
      {"status":"completed","final_result":"..."}
    """
    fr = payload.get("final_result")
    if fr is None:
        return None
    if isinstance(fr, str):
        s = fr.strip()
        return s or None
    if isinstance(fr, dict):
        for k in ("result", "final", "text", "message", "output"):
            v = fr.get(k)
            if isinstance(v, str) and v.strip():
                return v.strip()
        return _json_compact(fr)
    return None

def handle_default_route(prompt: str, user_id: int) -> str:
    """
    Flow:
      1) POST /executions -> execution_id (202)
      2) Poll /executions/{id}/status until terminal
      3) GET /executions/{id} -> extract final text
    """
    body = {
        "goal": prompt,
        "user_id": user_id,
        "max_depth": 1,
        "config_overrides": {"observability": {"mlflow": {"enabled": False}}},
    }

    try:
        resp = post_json(body, timeout=15)
        if not (200 <= resp.status_code < 300):
            return UX["backend_err"].format(code=resp.status_code, body=(resp.text or "")[:2000])

        try:
            j = resp.json()
        except Exception:
            j = {}

        execution_id = j.get("execution_id")
        if not execution_id:
            return UX["success_no_text"].format(body=f"(no execution_id)\n{_json_compact(j)}")

        logger.info(f"[BOT] started execution_id={execution_id}")

        t0 = time.monotonic()
        poll_every = float(RUNTIME.get("poll_interval", 1.0))
        max_wait = float(RUNTIME.get("max_wait", 30.0))
        terminal = {"completed", "failed", "timeout", "timed_out", "cancelled"}
        last_status: Optional[str] = None

        while True:
            status_payload = _get_status(execution_id)
            status = (status_payload.get("status") or "").lower()
            if status != last_status:
                logger.info(f"[BOT] {execution_id} status={status}")
                last_status = status

            if status in terminal:
                break
            if time.monotonic() - t0 > max_wait:
                return f"⚠️ Backend timed out waiting for result (last status={status or 'unknown'})."

            time.sleep(poll_every)

        result_payload = _get_result(execution_id)
        text = _extract_final_text_from_payload(result_payload)
        if text:
            return text

        compact = _json_compact({k: result_payload.get(k) for k in ("status", "final_result")})
        return UX["success_no_text"].format(body=compact)

    except requests.exceptions.Timeout:
        return UX["timeout"]
    except requests.exceptions.RequestException as e:
        return f"{UX['network_err']}\n\nDetails: {e}"
    except Exception as e:
        logger.exception("Unexpected error in default route")
        return f"❌ Unexpected error: {e}"


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(UX["start"])

async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(UX["help"])

async def privacy(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(UX["privacy"])

async def show(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        UX["show"].format(
            url=RUNTIME["api_url"],
            base=RUNTIME["api_base"],
            headers=json.dumps(RUNTIME["headers"], ensure_ascii=False)
        )
    )

async def seturl(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args:
        await update.message.reply_text(UX["seturl_usage"])
        return
    url = " ".join(context.args).strip()
    base = url[:-len("/executions")] if url.endswith("/executions") else url
    RUNTIME["api_url"] = url
    RUNTIME["api_base"] = base
    await update.message.reply_text(UX["seturl_ok"].format(url=url, base=base))

async def setheaders(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    raw = update.message.text.partition(" ")[2].strip()
    if not raw:
        await update.message.reply_text(UX["setheaders_usage"])
        return
    try:
        headers = json.loads(raw)
        if not isinstance(headers, dict):
            raise ValueError("Headers must be a JSON object.")
        RUNTIME["headers"] = headers
        await update.message.reply_text(
            UX["setheaders_ok"].format(headers=json.dumps(headers, ensure_ascii=False, indent=2))
        )
    except Exception:
        await update.message.reply_text(UX["setheaders_err"])

async def raw(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    body_txt = update.message.text.partition(" ")[2].strip()
    if not body_txt:
        await update.message.reply_text(UX["raw_usage"])
        return

    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        pass

    try:
        body = json.loads(body_txt)
        if not isinstance(body, dict):
            raise ValueError("Top-level JSON must be an object.")
    except Exception as e:
        await update.message.reply_text(f"❌ JSON parse error: {e}")
        return

    try:
        resp = post_json(body)
        status = resp.status_code
        text_body = resp.text or ""
        try:
            j = resp.json()
            pretty = _json_compact(j, limit=3500)
            msg = f"Status: {status}\n{pretty}"
        except ValueError:
            if len(text_body) > 3500:
                text_body = text_body[:3500] + " …"
            msg = f"Status: {status}\n{text_body if text_body else '(empty)'}"
        if len(msg) > 3900:
            msg = msg[:3900] + " …"
        await update.message.reply_text(msg)
    except requests.exceptions.Timeout:
        await update.message.reply_text(UX["timeout"])
    except requests.exceptions.RequestException:
        await update.message.reply_text(UX["network_err"])
    except Exception as e:
        logger.exception("Unexpected error in /raw")
        await update.message.reply_text(f"❌ Unexpected error: {e}")

async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    url = f"{RUNTIME['api_base']}/health"
    try:
        r = RUNTIME["session"].get(url, headers=_headers_no_ct(), timeout=5)
        await update.message.reply_text(UX["ping_ok"].format(url=url, code=r.status_code))
    except requests.exceptions.Timeout:
        await update.message.reply_text(UX["timeout"])
    except requests.exceptions.RequestException as e:
        await update.message.reply_text(UX["ping_err"].format(url=url, details=str(e)))

async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user_msg_raw = (update.message.text or "").strip()
    user_msg, warn = _soft_trim_user_text(user_msg_raw)
    user_id = update.message.from_user.id

    try:
        await update.effective_chat.send_action(ChatAction.TYPING)
    except Exception:
        pass

    reply = handle_default_route(user_msg, user_id)
    if len(reply) > 4000:
        reply = reply[:4000] + " …"

    if warn:
        await update.message.reply_text(warn)
    await update.message.reply_text(reply)

async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(UX["unknown_cmd"])


def main() -> None:
    token = TELEGRAM_BOT_TOKEN.strip()
    if not token or token == "PUT_YOUR_TELEGRAM_BOT_TOKEN_HERE":
        raise RuntimeError("Please set TELEGRAM_BOT_TOKEN")

    RUNTIME["session"] = build_session()

    app = ApplicationBuilder().token(token).build()


    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("privacy", privacy))
    app.add_handler(CommandHandler("show", show))
    app.add_handler(CommandHandler("seturl", seturl))
    app.add_handler(CommandHandler("setheaders", setheaders))
    app.add_handler(CommandHandler("raw", raw))
    app.add_handler(CommandHandler("ping", ping))


    app.add_handler(MessageHandler(filters.COMMAND, unknown))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    logger.info("Bot is running with long polling…")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
