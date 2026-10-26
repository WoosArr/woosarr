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
import os
import sys
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from woosarr.services import self_resolve

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
        self._cpu: tuple[int, ...] | None = None  # host CPU counters at the last tick

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
        cpu_before, self._cpu = self._cpu, cpu_times()
        if self._last_tick is not None:
            overslept = now - self._last_tick - self.check
            if overslept > self.stall:
                # Nothing ran, this thread included: the loop's silence is part of the same pause.
                self._record(Stall("paused", wall - overslept, wall))
                log.warning("WooSarr was paused for %.1f s: nothing in the process ran (the host was out of CPU, "
                            "disk or memory, or a long operation held Python's lock). Discord commands in that "
                            "time were answered late. %s%s WooSarr %s.", overslept, host_pressure(),
                            cpu_during(cpu_before, self._cpu), cpu_access())
                self._beat, self._blocked_since, self._blocked_wall = now, None, None
                self._last_tick = now
                self_resolve.recovered("loop")
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
            self_resolve.recovered("loop")

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


def _ranges(cores: list[int]) -> str:
    """[0, 1, 2, 3, 8] -> "0-3,8"."""
    out, start, prev = [], None, None
    for c in sorted(cores) + [None]:
        if start is None:
            start = prev = c
        elif c is not None and c == prev + 1:
            prev = c
        else:
            out.append(f"{start}-{prev}" if prev != start else str(start))
            start = prev = c
    return ",".join(out)


def cpu_quota(cgroup: Path = Path("/sys/fs/cgroup")) -> float | None:
    """A CPU limit in cores (docker --cpus / Unraid "Extra Parameters"), None when unlimited."""
    try:  # cgroup v2: "max 100000" or "200000 100000"
        quota, period = (cgroup / "cpu.max").read_text().split()[:2]
        return None if quota == "max" else int(quota) / int(period)
    except (OSError, ValueError):
        pass
    try:  # cgroup v1
        quota = int((cgroup / "cpu" / "cpu.cfs_quota_us").read_text())
        period = int((cgroup / "cpu" / "cpu.cfs_period_us").read_text())
        return None if quota <= 0 else quota / period
    except (OSError, ValueError):
        return None


def cpu_access() -> str:
    """How much CPU WooSarr may use: host cores, the cores it's pinned to, and any quota."""
    host = os.cpu_count() or 0
    try:
        allowed = sorted(os.sched_getaffinity(0))
    except (AttributeError, OSError):  # not Linux
        allowed = list(range(host))
    pinned = f" (cores {_ranges(allowed)})" if allowed and len(allowed) < host else ""
    quota = cpu_quota()
    limit = f", limited to {quota:g} cores' worth of time" if quota is not None else ", no CPU time limit"
    return f"may run on {len(allowed)} of the host's {host} CPU cores{pinned}{limit}"


def cpu_times(stat: Path = Path("/proc/stat")) -> tuple[int, ...] | None:
    """The host's total CPU counters (user nice system idle iowait irq softirq steal), Linux only."""
    try:
        return tuple(int(x) for x in stat.read_text().split("\n", 1)[0].split()[1:9])
    except (OSError, ValueError):
        return None


def cpu_during(before: tuple[int, ...] | None, after: tuple[int, ...] | None) -> str:
    """What the host's CPUs did between two samples: busy, waiting on disk, taken by a hypervisor."""
    if not before or not after or len(before) < 8 or len(after) < 8:
        return ""
    d = [b - a for a, b in zip(before, after, strict=False)]
    total = sum(d)
    if total <= 0:
        return ""
    user, nice, system, idle, iowait, irq, softirq, steal = d[:8]

    def pct(v: int) -> int:
        return round(100 * v / total)

    return (f" During the pause the host's {os.cpu_count() or '?'} CPUs were {pct(user + nice + system + irq + softirq)}% "
            f"busy, {pct(iowait)}% waiting on disk, {pct(steal)}% taken by a hypervisor and {pct(idle)}% idle.")


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
