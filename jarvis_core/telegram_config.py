"""Environment-backed configuration for the Jarvis Telegram integration."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

from dotenv import load_dotenv

from jarvis_core.config import BASE_DIR


def parse_allowed_user_ids(raw: str) -> frozenset[int]:
    """Parse a comma/semicolon/whitespace separated list of Telegram user IDs."""
    if not raw or not raw.strip():
        return frozenset()

    values = [value for value in re.split(r"[,;\s]+", raw.strip()) if value]
    try:
        parsed = frozenset(int(value) for value in values)
    except ValueError as exc:
        raise ValueError(
            "TELEGRAM_ALLOWED_USER_IDS must contain only numeric Telegram user IDs."
        ) from exc

    if any(user_id <= 0 for user_id in parsed):
        raise ValueError("TELEGRAM_ALLOWED_USER_IDS must contain positive user IDs.")
    return parsed


@dataclass(frozen=True)
class TelegramSettings:
    """Validated Telegram settings. The token is excluded from repr output."""

    bot_token: str = field(default="", repr=False)
    allowed_user_ids: frozenset[int] = field(default_factory=frozenset)
    poll_timeout_seconds: int = 20
    completion_timeout_seconds: int = 600

    @property
    def enabled(self) -> bool:
        return bool(self.bot_token and self.allowed_user_ids)

    @classmethod
    def from_env(cls) -> "TelegramSettings":
        load_dotenv(BASE_DIR / ".env", override=False)
        return cls(
            bot_token=os.getenv("TELEGRAM_BOT_TOKEN", "").strip(),
            allowed_user_ids=parse_allowed_user_ids(
                os.getenv("TELEGRAM_ALLOWED_USER_IDS", "")
            ),
        )
