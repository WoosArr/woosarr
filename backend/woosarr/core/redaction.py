"""Log redaction (N7). Attach RedactingFilter to every handler."""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path

REDACTED = "[REDACTED]"
# Bump when the patterns gain something files already written may contain: scrub_files() then
# runs once more over the existing log files (core/logs.py, at startup).
VERSION = 2

_PATTERNS = [
    # Plex tokens in URLs/headers/JSON
    re.compile(r"(?i)(x-plex-token['\"]?\s*[:=]\s*['\"]?)([A-Za-z0-9_\-]{8,})"),
    re.compile(r"(?i)([?&]token=)([^&\s'\"]+)"),
    # Credentials and one-time values in other URL query strings: the OIDC callback (code, state,
    # session_state; live 2026-10-27, uvicorn.access), signed download links (sig, hmac, X-Amz-*;
    # GitHub's package storage for the update check), and token-like parameters.
    re.compile(r"(?i)([?&](?:code|state|session_state|id_token|access_token|refresh_token|code_verifier|"
               r"client_secret|nonce|sig|signature|hmac|x-amz-signature|x-amz-credential|"
               r"x-amz-security-token|api_?key|key)=)([^&\s'\"]+)"),
    # Authorization headers
    re.compile(r"(?i)(authorization['\"]?\s*[:=]\s*['\"]?(?:bearer|bot)\s+)([A-Za-z0-9._\-]+)"),
    # "token": "..." / 'token': '...' in JSON or dict reprs (e.g. Discord interaction payloads)
    re.compile(r"""(?i)(['"](?:token|access_token|refresh_token|id_token|authToken|accessToken|client_secret|code_verifier)['"]\s*:\s*['"])([^'"]+)"""),
    # Discord interaction/webhook URLs carry the token in the path
    re.compile(r"(/(?:interactions|webhooks)/\d+/)([A-Za-z0-9_\-.]+)"),
    # Generic key=value secrets
    re.compile(r"(?i)((?:password|passwd|secret|api[_-]?key|access[_-]?token)['\"]?\s*[:=]\s*['\"]?)([^\s'\",}]+)"),
]
# WooSarr API keys (core/auth.py)
_API_KEY = re.compile(r"\bwsk_[0-9a-f]{8}_[A-Za-z0-9_\-]{20,}")
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
    text = _API_KEY.sub(REDACTED, text)
    return _DISCORD_TOKEN.sub(REDACTED, text)


# Control characters in a logged value (CodeQL py/log-injection): a username, a URL segment or an
# error text from outside could otherwise start what looks like a new log line in `docker logs` or
# carry terminal escapes. Logs are stored as one JSON object per line, so this is about readability
# and the console, not the stored entries. Tab stays; everything else is shown escaped.
_CONTROL = {c: f"\\x{c:02x}" for c in (*range(0x20), 0x7F) if c != 0x09}
_CONTROL |= {0x0A: "\\n", 0x0D: "\\r", 0x85: "\\x85", 0x2028: "\\u2028", 0x2029: "\\u2029"}


def escape_controls(text: str) -> str:
    return text.translate(_CONTROL)


class Multiline(str):
    """A logged value WooSarr built itself whose line breaks are wanted (a stack trace)."""

    __slots__ = ()


class _Escaped:
    """A log argument shown with its control characters escaped, whether formatted with %s or %r."""

    __slots__ = ("value",)

    def __init__(self, value: object) -> None:
        self.value = value

    def __str__(self) -> str:
        return escape_controls(str(self.value))

    def __repr__(self) -> str:
        return escape_controls(repr(self.value))


def _escape_arg(value: object) -> object:
    if value is None or isinstance(value, (int, float, Multiline)):  # numbers keep %d / %.1f working
        return value
    return _Escaped(value)


def escape_args(record: logging.LogRecord) -> None:
    """Escape control characters in the values of WooSarr's own log calls (not the message text,
    which is ours, and not libraries' records, whose tracebacks are passed as values)."""
    if not record.args or not record.name.startswith("woosarr"):
        return
    if isinstance(record.args, Mapping):
        record.args = {k: _escape_arg(v) for k, v in record.args.items()}
    else:
        record.args = tuple(_escape_arg(v) for v in record.args)


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        escape_args(record)
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


def scrub_line(line: str) -> str:
    """One line of a JSON log file, redacted with today's patterns (text fields only, so the
    JSON stays valid); lines that aren't JSON are redacted as they are."""
    try:
        entry = json.loads(line)
    except ValueError:
        return redact(line)
    if not isinstance(entry, dict):
        return line
    changed = False
    for field in ("message", "exc"):
        value = entry.get(field)
        if isinstance(value, str) and (clean := redact(value)) != value:
            entry[field], changed = clean, True
    return json.dumps(entry, ensure_ascii=False) if changed else line


def scrub_file(path: Path) -> int:
    """Rewrites a log file without what redact() removes now. Returns how many lines changed."""
    changed = 0
    tmp = path.with_name(path.name + ".scrub")
    with path.open(encoding="utf-8", errors="replace") as src, tmp.open("w", encoding="utf-8") as dst:
        for raw in src:
            line = raw.rstrip("\n")
            clean = scrub_line(line)
            changed += clean != line
            dst.write(clean + "\n")
    if changed:
        os.replace(tmp, path)
    else:
        tmp.unlink()
    return changed


def scrub_files(log_dir: Path) -> dict[str, int]:
    """Once per VERSION: removes from the existing log files (woosarr.log*, alerts.log*) what
    the current patterns redact, so secrets logged before a pattern existed don't stay on disk.
    Runs before logging opens the files. Returns {file: lines changed} for those that changed."""
    marker = log_dir / ".redaction-version"
    try:
        if marker.read_text().strip() == str(VERSION):
            return {}
    except OSError:
        pass
    result: dict[str, int] = {}
    for path in sorted(log_dir.glob("*.log*")):
        if path.is_file() and not path.name.endswith(".scrub") and (n := scrub_file(path)):
            result[path.name] = n
    marker.write_text(f"{VERSION}\n")
    return result


def install(handler: logging.Handler) -> None:
    handler.addFilter(RedactingFilter())
