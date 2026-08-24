"""Minimal, testable Telegram Bot API client used by Jarvis long polling."""
from __future__ import annotations

from typing import Any, Optional

import requests


class TelegramAPIError(RuntimeError):
    """Raised for a sanitized Telegram API or transport failure.

    The bot token is never embedded in the message. Safe error metadata is
    attached as attributes so the long-poll loop can react to specific,
    non-sensitive failure classes -- notably a HTTP 409 ``getUpdates`` conflict
    that indicates a duplicate polling worker -- without logging secrets.
    """

    def __init__(
        self,
        message: str,
        *,
        status_code: Optional[int] = None,
        error_type: Optional[str] = None,
        is_duplicate_poller: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.error_type = error_type
        self.is_duplicate_poller = is_duplicate_poller


def _duplicate_poller_conflict(status_code, description) -> bool:
    """True when a Telegram call failed because another long-poll owns this token."""
    if status_code != 409:
        return False
    lowered = str(description or "").lower()
    return "getupdates" in lowered and "conflict" in lowered


def _extract_description(response) -> str:
    """Return the safe ``description`` from a Telegram error response body."""
    try:
        data = response.json()
    except ValueError:
        return ""
    if isinstance(data, dict):
        return str(data.get("description") or "")
    return ""


class TelegramBotAPI:
    def __init__(self, token: str, session: Optional[requests.Session] = None) -> None:
        if not token:
            raise ValueError("Telegram bot token is required")
        self._base_url = f"https://api.telegram.org/bot{token}"
        self._session = session or requests.Session()

    def _request(self, method: str, *, http_method: str = "post", **kwargs: Any) -> Any:
        url = f"{self._base_url}/{method}"
        try:
            request = getattr(self._session, http_method)
            response = request(url, **kwargs)
            response.raise_for_status()
            payload = response.json()
        except requests.HTTPError as exc:
            status = None
            description = ""
            if exc.response is not None:
                status = exc.response.status_code
                description = _extract_description(exc.response)
            raise TelegramAPIError(
                f"Telegram API request failed "
                f"(HTTP {status or 'unknown'} / {type(exc).__name__}).",
                status_code=status,
                error_type=type(exc).__name__,
                is_duplicate_poller=_duplicate_poller_conflict(status, description),
            ) from exc
        except requests.RequestException as exc:
            raise TelegramAPIError(
                f"Telegram API request failed ({type(exc).__name__}).",
                error_type=type(exc).__name__,
            ) from exc
        except ValueError as exc:
            raise TelegramAPIError(
                f"Telegram API returned an invalid response ({type(exc).__name__}).",
                error_type=type(exc).__name__,
            ) from exc

        if not payload.get("ok"):
            description = str(payload.get("description") or "Telegram API rejected the request")
            status = payload.get("error_code")
            raise TelegramAPIError(
                description[:300],
                status_code=status if isinstance(status, int) else None,
                error_type="Telegram API rejection",
                is_duplicate_poller=_duplicate_poller_conflict(status, description),
            )
        return payload.get("result")

    def get_updates(self, *, offset: Optional[int], timeout: int) -> list[dict]:
        params: dict[str, Any] = {
            "timeout": max(0, int(timeout)),
            "allowed_updates": '["message","callback_query"]',
        }
        if offset is not None:
            params["offset"] = offset
        result = self._request(
            "getUpdates",
            http_method="get",
            params=params,
            timeout=(10, max(15, int(timeout) + 10)),
        )
        return result if isinstance(result, list) else []

    def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_markup: Optional[dict] = None,
    ) -> dict:
        data: dict[str, Any] = {"chat_id": chat_id, "text": text}
        if reply_markup is not None:
            data["reply_markup"] = reply_markup
        result = self._request("sendMessage", json=data, timeout=(10, 30))
        return result if isinstance(result, dict) else {}

    def answer_callback_query(self, callback_query_id: str, text: str = "") -> None:
        data = {"callback_query_id": callback_query_id}
        if text:
            data["text"] = text
        self._request("answerCallbackQuery", json=data, timeout=(10, 30))

    def close(self) -> None:
        self._session.close()
