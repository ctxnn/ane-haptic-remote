"""A small Telegram Bot API client on the standard library (long polling, JSON)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple


class TelegramError(RuntimeError):
    def __init__(
        self,
        method: str,
        description: str,
        error_code: Optional[int] = None,
        retry_after: Optional[float] = None,
    ) -> None:
        super().__init__(f"{method}: {description}")
        self.method = method
        self.description = description
        self.error_code = error_code
        self.retry_after = retry_after


class TelegramAPI:
    def __init__(
        self,
        token: str,
        base_url: str = "https://api.telegram.org",
        timeout_s: float = 15.0,
        opener: Callable[..., Any] = urllib.request.urlopen,
    ) -> None:
        if not token:
            raise ValueError("a bot token is required")
        self._token = token
        self._base = base_url.rstrip("/")
        self.timeout_s = timeout_s
        self._opener = opener

    def call(self, method: str, params: Optional[Dict[str, Any]] = None, timeout_s: Optional[float] = None) -> Any:
        body = json.dumps({k: v for k, v in (params or {}).items() if v is not None}).encode("utf-8")
        request = urllib.request.Request(
            f"{self._base}/bot{self._token}/{method}",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener(request, timeout=timeout_s or self.timeout_s) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                payload = json.loads(exc.read().decode("utf-8"))
            except (ValueError, OSError):
                raise TelegramError(method, f"HTTP {exc.code}", exc.code) from None
        except ValueError:
            raise TelegramError(method, "invalid JSON from Telegram") from None
        if not payload.get("ok"):
            parameters = payload.get("parameters") or {}
            raise TelegramError(
                method,
                str(payload.get("description", "unknown error")),
                payload.get("error_code"),
                parameters.get("retry_after"),
            )
        return payload.get("result")

    def get_me(self) -> Dict[str, Any]:
        return self.call("getMe")

    def get_updates(self, offset: Optional[int] = None, timeout: int = 20) -> List[Dict[str, Any]]:
        return self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "callback_query"]},
            timeout_s=timeout + 10,
        )

    def send_message(self, chat_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )

    def edit_message_text(
        self, chat_id: int, message_id: int, text: str, reply_markup: Optional[Dict[str, Any]] = None
    ) -> Any:
        return self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": reply_markup,
            },
        )

    def answer_callback_query(self, callback_query_id: str, text: Optional[str] = None) -> Any:
        return self.call("answerCallbackQuery", {"callback_query_id": callback_query_id, "text": text})

    def set_my_commands(self, commands: Sequence[Tuple[str, str]]) -> Any:
        return self.call(
            "setMyCommands",
            {"commands": [{"command": name, "description": text} for name, text in commands]},
        )
