"""Send new-listing alerts to a Telegram chat through the user's own bot.

Settings live in data/telegram.json (outside Git):

    {"token": "<from @BotFather>", "chat_id": 123456789}

The token can also come from the AVITO_TELEGRAM_TOKEN environment variable.
``avito.py telegram setup`` finds the chat id after you send /start to the bot.
"""

from __future__ import annotations

import html
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SETTINGS_PATH = Path(__file__).resolve().parent / "data" / "telegram.json"
API = "https://api.telegram.org/bot{token}/{method}"
TIMEOUT_SECONDS = 15
NETWORK_ATTEMPTS = 3
RETRY_PAUSE_SECONDS = 3


class TelegramError(RuntimeError):
    pass


def load_settings(path: Path = SETTINGS_PATH) -> dict[str, Any]:
    try:
        settings = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        settings = {}
    except (OSError, json.JSONDecodeError) as exc:
        raise TelegramError(f"не читается {path}: {exc}") from None
    token = os.environ.get("AVITO_TELEGRAM_TOKEN") or settings.get("token")
    return {"token": token, "chat_id": settings.get("chat_id")}


def save_settings(settings: dict[str, Any], path: Path = SETTINGS_PATH) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass


def call(token: str, method: str, payload: dict[str, Any] | None = None) -> Any:
    """One Bot API call; a network hiccup is retried before giving up."""
    for attempt in range(NETWORK_ATTEMPTS):
        try:
            return _call_once(token, method, payload)
        except TelegramNetworkError:
            if attempt == NETWORK_ATTEMPTS - 1:
                raise
            time.sleep(RETRY_PAUSE_SECONDS)
    raise AssertionError("unreachable")


class TelegramNetworkError(TelegramError):
    pass


def _call_once(token: str, method: str, payload: dict[str, Any] | None) -> Any:
    request = urllib.request.Request(
        API.format(token=token, method=method),
        data=json.dumps(payload or {}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as response:
            answer = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            description = json.loads(exc.read().decode("utf-8")).get("description")
        except (ValueError, OSError):
            description = None
        raise TelegramError(f"Telegram ответил {exc.code}: {description or exc.reason}") from None
    except (urllib.error.URLError, TimeoutError) as exc:
        raise TelegramNetworkError(f"нет связи с Telegram: {exc}") from None
    if not answer.get("ok"):
        raise TelegramError(f"Telegram: {answer.get('description')}")
    return answer.get("result")


def find_chat_id(token: str) -> tuple[int, str]:
    """The chat that most recently wrote to the bot (send it /start first)."""
    updates = call(token, "getUpdates", {"timeout": 0})
    for update in reversed(updates or []):
        message = update.get("message") or update.get("my_chat_member") or {}
        chat = message.get("chat") or {}
        if "id" in chat:
            name = chat.get("title") or chat.get("username") or chat.get("first_name") or str(chat["id"])
            return int(chat["id"]), str(name)
    raise TelegramError("бот пока не получил ни одного сообщения: напишите ему /start и повторите")


class Notifier:
    def __init__(self, token: str, chat_id: int) -> None:
        self.token = token
        self.chat_id = chat_id

    @classmethod
    def from_settings(cls) -> "Notifier | None":
        settings = load_settings()
        if settings.get("token") and settings.get("chat_id"):
            return cls(settings["token"], int(settings["chat_id"]))
        return None

    def send(self, text: str) -> None:
        call(self.token, "sendMessage", {
            "chat_id": self.chat_id,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": False,
        })


def esc(value: Any) -> str:
    return html.escape(str(value or ""))


def realty_message(search_title: str, alert: dict[str, Any]) -> str:
    lines = [
        f"🏠 <b>{esc(alert['title'])}</b>",
        f"💰 <b>{esc(alert['price'])}</b> · {esc(alert['price_per_m2'])}/м²",
        f"📍 {esc(alert['address'])}" + (f" · м. {esc(alert['metro'])}" if alert.get("metro") else ""),
        f"🕒 {esc(alert['published'])} · {esc(alert['verdict'])}: {esc('; '.join(alert['reasons']))}",
    ]
    if alert.get("notes"):
        lines.append("⚠️ " + esc("; ".join(alert["notes"])))
    lines.append(f'<a href="{esc(alert["url"])}">Открыть на Авито</a>')
    if alert.get("draft"):
        lines.append(f"\n✉️ <i>Черновик:</i>\n<code>{esc(alert['draft'])}</code>")
    lines.append(f"\n#{esc(search_title.replace('-', '_'))}")
    return "\n".join(lines)


def goods_message(search_title: str, alert: dict[str, Any]) -> str:
    market = ""
    if alert.get("vs_market"):
        market = f" · рынок {esc(alert['market'])} ({esc(alert['vs_market'])})"
    lines = [
        f"📱 <b>{esc(alert['title'])}</b>",
        f"💰 <b>{esc(alert['price'])}</b>{market}",
    ]
    if alert.get("avito_badge"):
        lines.append(f"🏷 Авито: {esc(alert['avito_badge'])}")
    seller = esc(alert["seller"])
    if alert.get("closed_listings") is not None:
        seller += f", завершённых объявлений: {esc(alert['closed_listings'])}"
    lines.append(f"👤 {seller}")
    if alert.get("notes"):
        lines.append("⚠️ " + esc("; ".join(alert["notes"])))
    lines.append(f'<a href="{esc(alert["url"])}">Открыть на Авито</a>')
    lines.append(f"\n#{esc(search_title.replace('-', '_'))}")
    return "\n".join(lines)


def message_for(profile: str, search_title: str, alert: dict[str, Any]) -> str:
    if profile == "realty-owner":
        return realty_message(search_title, alert)
    return goods_message(search_title, alert)
