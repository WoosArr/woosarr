"""Logging setup (plan P1-05, N7).

Every sink gets a RedactingFilter:
  * stderr (container logs), JSON lines or text
  * <config>/logs/woosarr.log, rotating, survives restarts
  * an in-memory ring buffer served by the logs API, the main way to diagnose a
    deployed instance remotely

configure_logging() is idempotent: calling it again replaces the handlers it
installed earlier instead of stacking duplicates.
"""

from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import threading
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from woosarr.core import redaction
from woosarr.core.config import Settings

# Libraries whose own loggers should flow through our (redacting) root handlers.
_ROUTED_LOGGERS = ("uvicorn", "uvicorn.error", "uvicorn.access", "fastapi", "discord", "plexapi")


@dataclass(frozen=True)
class LogEntry:
    seq: int
    ts: str
    level: str
    logger: str
    message: str
    exc: str | None = None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _timestamp(record: logging.LogRecord) -> str:
    return datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds")


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "ts": _timestamp(record),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        exc = self.formatException(record.exc_info) if record.exc_info else record.exc_text
        if exc:
            payload["exc"] = exc
        return json.dumps(payload, ensure_ascii=False)


TEXT_FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


class RingBufferHandler(logging.Handler):
    """Keeps the most recent records in memory with a monotonically rising seq.

    Warnings and errors are also kept in a separate ring, so a busy INFO log
    can't push them out before the dashboard's Alerts have shown them. At startup
    that ring is refilled from the log files, so alerts survive a restart.
    """

    PROBLEMS_CAPACITY = 2000

    def __init__(self, capacity: int) -> None:
        super().__init__()
        self._entries: collections.deque[LogEntry] = collections.deque(maxlen=capacity)
        self._problems: collections.deque[LogEntry] = collections.deque(maxlen=self.PROBLEMS_CAPACITY)
        self._seq = 0
        self._entries_lock = threading.Lock()

    def preload(self, entries: list[LogEntry]) -> None:
        """Earlier warnings/errors (from the log files), oldest first; given fresh seq numbers."""
        with self._entries_lock:
            for e in entries[-self.PROBLEMS_CAPACITY:]:
                self._seq += 1
                self._problems.append(LogEntry(self._seq, e.ts, e.level, e.logger, e.message, e.exc))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            message = record.getMessage()
            exc = record.exc_text
            if record.exc_info and not exc:
                exc = logging.Formatter().formatException(record.exc_info)
            with self._entries_lock:
                self._seq += 1
                entry = LogEntry(self._seq, _timestamp(record), record.levelname, record.name, message, exc)
                self._entries.append(entry)
                if record.levelno >= logging.WARNING:
                    self._problems.append(entry)
        except RecursionError:
            raise
        # Same contract as logging.Handler.emit: never let a log call crash the app.
        except Exception:  # noqa: BLE001
            self.handleError(record)

    def query(
        self,
        since: int = 0,
        min_level: int = logging.NOTSET,
        contains: str | None = None,
        limit: int = 500,
    ) -> tuple[list[LogEntry], int]:
        """Entries with seq > since, oldest first, plus the latest seq for the next poll."""
        with self._entries_lock:
            # WARNING and up: the problems ring holds more of them than the main ring.
            snapshot = list(self._problems if min_level >= logging.WARNING else self._entries)
            latest = self._seq
        needle = contains.lower() if contains else None
        matches = [
            e
            for e in snapshot
            if e.seq > since
            and logging.getLevelName(e.level) >= min_level
            and (needle is None or needle in e.message.lower() or needle in e.logger.lower())
        ]
        return matches[-limit:], latest


class _State:
    def __init__(self) -> None:
        self.handlers: list[logging.Handler] = []
        self.buffer: RingBufferHandler | None = None


_state = _State()


def buffer() -> RingBufferHandler:
    if _state.buffer is None:
        raise RuntimeError("logging not configured; call configure_logging() first")
    return _state.buffer


# discord.py at DEBUG dumps every gateway event and HTTP body (member lists,
# interaction payloads). WooSarr's own DEBUG logs say what matters, so these stay
# at INFO even when the app log level is DEBUG.
CAPPED_LOGGERS = ("discord", "discord.http", "discord.gateway", "discord.client", "discord.webhook",
                  "charset_normalizer")


def cap_library_loggers() -> None:
    for name in CAPPED_LOGGERS:
        logging.getLogger(name).setLevel(logging.INFO)


_PROBLEM_LEVELS = ('"level": "WARNING"', '"level": "ERROR"', '"level": "CRITICAL"')


def read_problems(log_file: Path, backups: int) -> list[LogEntry]:
    """WARNING and up from the JSON log file and its rotated backups, oldest first.

    The files were written through the redacting handlers, so nothing secret is re-read.
    """
    out: list[LogEntry] = []
    files = [log_file.with_name(f"{log_file.name}.{i}") for i in range(backups, 0, -1)] + [log_file]
    for path in files:
        try:
            with path.open(encoding="utf-8", errors="replace") as fh:
                for line in fh:
                    if not any(level in line for level in _PROBLEM_LEVELS):
                        continue
                    try:
                        d = json.loads(line)
                        out.append(LogEntry(0, str(d["ts"]), str(d["level"]), str(d["logger"]), str(d["message"]),
                                            d.get("exc")))
                    except (ValueError, KeyError, TypeError):
                        continue
        except OSError:
            continue
    return out


def configure_logging(settings: Settings) -> RingBufferHandler:
    root = logging.getLogger()
    for handler in _state.handlers:
        root.removeHandler(handler)
        handler.close()

    formatter: logging.Formatter = (
        JsonFormatter() if settings.log_format == "json" else logging.Formatter(TEXT_FORMAT)
    )

    stream = logging.StreamHandler()
    stream.setFormatter(formatter)

    settings.log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.handlers.RotatingFileHandler(
        settings.log_file,
        maxBytes=settings.log_file_max_bytes,
        backupCount=settings.log_file_backups,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())

    ring = RingBufferHandler(settings.log_buffer_size)
    earlier = read_problems(settings.log_file, settings.log_file_backups)
    ring.preload(earlier)

    handlers: list[logging.Handler] = [stream, file_handler, ring]
    for handler in handlers:
        redaction.install(handler)
        root.addHandler(handler)
    root.setLevel(settings.log_level)

    # Make library loggers propagate to the redacting root handlers instead of
    # writing through handlers of their own.
    for name in _ROUTED_LOGGERS:
        lib_logger = logging.getLogger(name)
        lib_logger.handlers.clear()
        lib_logger.propagate = True

    cap_library_loggers()
    _state.handlers = handlers
    _state.buffer = ring
    if earlier:
        logging.getLogger("woosarr.logs").info("reloaded %d earlier warnings/errors from the log files for Alerts",
                                               min(len(earlier), ring.PROBLEMS_CAPACITY))
    return ring
