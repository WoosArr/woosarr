"""Notices when WooSarr stops running, and says why.

A thread wakes every CHECK_SECONDS and asks the event loop to note the time. Two kinds of stall:

- **blocked**: the thread runs but the loop doesn't answer, so code on the loop is busy. The
  loop thread's stack is logged once per stall: it names the culprit.
- **paused**: the thread itself woke up late, so nothing in the process ran (the host had no CPU,
  disk or memory to give, or a long operation held Python's lock). Host load, memory pressure and
  the process's swap use are logged.

Recent stalls are kept so a late Discord command can be blamed on WooSarr, not on Discord.
Live 2026-10-24: a 14 s gap with no log lines at all made a /request look "delivered late".
"""

from __future__ import annotations

import asyncio
import logging
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("woosarr.stall")

CHECK_SECONDS = 0.5
STALL_SECONDS = 3.0


@dataclass(frozen=True)
class Stall:
    kind: str  # "blocked" | "paused"
    start: float  # wall clock (time.time())
    end: float

    @property
    def seconds(self) -> float:
        return self.end - self.start


class StallWatch:
    def __init__(self, check: float = CHECK_SECONDS, stall: float = STALL_SECONDS) -> None:
        self.check, self.stall = check, stall
        self.stalls: deque[Stall] = deque(maxlen=50)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread: int | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._beat = time.monotonic()  # last time the loop answered
        self._last_tick: float | None = None
        self._blocked_since: float | None = None  # monotonic, while the loop is blocked
        self._blocked_wall: float | None = None

    # ---- lifecycle -------------------------------------------------------------------------

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """Call from the event loop's thread."""
        self._loop, self._loop_thread = loop, threading.get_ident()
        self._beat = time.monotonic()
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="woosarr-stall-watch", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while not self._stop.wait(self.check):
            try:
                self.tick(time.monotonic(), time.time())
                if self._loop is not None and not self._loop.is_closed():
                    self._loop.call_soon_threadsafe(self._mark)
            except RuntimeError:
                return  # loop closed
            except Exception:  # the watchdog must never take anything down
                log.exception("stall watch failed")

    def _mark(self) -> None:
        self._beat = time.monotonic()

    # ---- the check (separate from the thread, so it can be tested with made-up times) --------

    def tick(self, now: float, wall: float) -> None:
        if self._last_tick is not None:
            overslept = now - self._last_tick - self.check
            if overslept > self.stall:
                # Nothing ran, this thread included: the loop's silence is part of the same pause.
                self._record(Stall("paused", wall - overslept, wall))
                log.warning("WooSarr was paused for %.1f s: nothing in the process ran (the host was out of CPU, "
                            "disk or memory, or a long operation held Python's lock). Discord commands in that "
                            "time were answered late. %s", overslept, host_pressure())
                self._beat, self._blocked_since, self._blocked_wall = now, None, None
                self._last_tick = now
                return
        self._last_tick = now
        behind = now - self._beat
        if behind > self.stall and self._blocked_since is None:
            self._blocked_since, self._blocked_wall = self._beat, wall - behind
            log.warning("WooSarr's event loop is blocked (%.1f s so far): Discord commands and the web UI wait "
                        "until it's free. It's running:\n%s", behind, self.loop_stack())
        elif behind <= self.check * 2 and self._blocked_since is not None:
            took = now - self._blocked_since
            self._record(Stall("blocked", self._blocked_wall or wall - took, wall))
            log.warning("WooSarr's event loop was blocked for %.1f s", took)
            self._blocked_since, self._blocked_wall = None, None

    def _record(self, stall: Stall) -> None:
        self.stalls.append(stall)

    def loop_stack(self) -> str:
        frame = sys._current_frames().get(self._loop_thread or -1)
        if frame is None:
            return "  (loop thread not found)"
        lines = traceback.format_stack(frame)[-12:]
        return "".join(lines).rstrip()

    # ---- for blaming late Discord commands ----------------------------------------------------

    def during(self, start: float, end: float) -> Stall | None:
        """The longest stall overlapping [start, end] (wall clock), or one still going on."""
        found = [s for s in self.stalls if s.start < end and s.end > start]
        if self._blocked_since is not None and self._blocked_wall is not None and self._blocked_wall < end:
            found.append(Stall("blocked", self._blocked_wall, time.time()))
        return max(found, key=lambda s: min(s.end, end) - max(s.start, start)) if found else None


def host_pressure() -> str:
    """Load and memory figures that explain a pause (Linux; empty elsewhere)."""
    parts = []
    try:
        parts.append("load " + " ".join(Path("/proc/loadavg").read_text().split()[:3]))
    except OSError:
        pass
    for name in ("cpu", "io", "memory"):
        try:
            some = Path(f"/proc/pressure/{name}").read_text().splitlines()[0].split()
            avg10 = next(p.split("=")[1] for p in some if p.startswith("avg10="))
            parts.append(f"{name} pressure {avg10}%")
        except (OSError, StopIteration, IndexError):
            pass
    try:
        status = dict(line.split(":", 1) for line in Path("/proc/self/status").read_text().splitlines() if ":" in line)
        parts.append(f"WooSarr memory {status['VmRSS'].strip()}, swapped out {status.get('VmSwap', '0 kB').strip()}")
    except (OSError, KeyError, ValueError):
        pass
    return ("Host: " + ", ".join(parts) + ".") if parts else ""


WATCH = StallWatch()
