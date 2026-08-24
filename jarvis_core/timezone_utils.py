"""Shared timezone conversions for Jarvis calendar-based behavior."""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo


def jarvis_timezone() -> tzinfo:
    """Return the timezone used by the running Jarvis host."""
    return datetime.now().astimezone().tzinfo or timezone.utc


def to_jarvis_datetime(value: datetime) -> datetime:
    """Interpret naive values as Jarvis-local and convert aware values."""
    zone = jarvis_timezone()
    if value.tzinfo is None:
        return value.replace(tzinfo=zone)
    return value.astimezone(zone)


def jarvis_now() -> datetime:
    return datetime.now(jarvis_timezone())
