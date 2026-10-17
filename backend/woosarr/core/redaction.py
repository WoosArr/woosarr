"""Log redaction (N7). Attach RedactingFilter to every handler."""

from __future__ import annotations

import logging
import re
import threading

REDACTED = "[REDACTED]"

_PATTERNS = [
    # Plex tokens in URLs/headers/JSON
    re.compile(r"(?i)(x-plex-token['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)([?&]token=)([^&\s'\"]+)"),
    # Authorization headers
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?(?:bearer|bot)\s+)([A-Za-z0-9._\-]+)"),
    # Generic key=value secrets
    re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token)['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"),
]
# Discord bot token shape: base64(id).timestamp.hmac
_DISCORD_TOKEN = re.compile(r"\b[A-Za-z0-9_\-]{23,28}\.[A-Za-z0-9_\-]{6,7}\.[A-Za-z0-9_\-]{27,}\b")


class _Registry:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._values: set[str] = set()

    def add(self, value: str | None) -> None:
        if value and len(value) >= 6:
            with self._lock:
                self._values.add(value)

    def values(self) -> list[str]:
        with self._lock:
            return sorted(self._values, key=len, reverse=True)

    def clear(self) -> None:
        with self._lock:
            self._values.clear()


registry = _Registry()


def register_secret(value: str | None) -> None:
    """Make sure an exact secret value is scrubbed wherever it shows up."""
    registry.add(value)


def redact(text: str) -> str:
    for value in registry.values():
        if value in text:
            text = text.replace(value, REDACTED)
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    return _DISCORD_TOKEN.sub(REDACTED, text)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
        # Deliberately broad: a malformed log call (bad format args, odd __str__)
        # must never crash logging itself.
        except Exception:  # noqa: BLE001
            message = str(record.msg)
        record.msg = redact(message)
        record.args = None
        if record.exc_info:
            # Render and scrub the traceback so secrets in exception text don't leak.
            formatter = logging.Formatter()
            record.exc_text = redact(formatter.formatException(record.exc_info))
            record.exc_info = None
        elif record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def install(handler: logging.Handler) -> None:
    handler.addFilter(RedactingFilter())
