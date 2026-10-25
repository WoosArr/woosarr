"""The stall watchdog: tells a blocked event loop (and what blocked it) from a paused process."""

import asyncio
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest import mock

import discord

from tests.test_requests import FakeInteraction
from woosarr.services import discord_timing, stall_watch
from woosarr.services.discord_timing import DiscordTiming, acknowledge
from woosarr.services.stall_watch import Stall, StallWatch


def busy_work_on_the_loop():
    time.sleep(0.9)  # stands in for code that blocks the event loop


class StallWatchTests(unittest.TestCase):
    def test_blocked_loop_names_what_it_was_running(self):
        watch = StallWatch(check=0.05, stall=0.3)

        async def main():
            watch.start(asyncio.get_running_loop())
            await asyncio.sleep(0.1)
            busy_work_on_the_loop()
            await asyncio.sleep(0.4)  # let the watchdog see the loop answer again
            watch.stop()

        with self.assertLogs("woosarr.stall", "WARNING") as logs:
            asyncio.run(main())
        self.assertIn("event loop is blocked", logs.output[0])
        self.assertIn("busy_work_on_the_loop", logs.output[0])
        self.assertRegex(logs.output[-1], r"was blocked for [01]\.\d s")
        self.assertEqual([s.kind for s in watch.stalls], ["blocked"])

    def test_paused_process_is_told_apart(self):
        watch = StallWatch(check=0.5, stall=3.0)
        watch.tick(100.0, 1000.0)
        watch._beat = 100.0
        with self.assertLogs("woosarr.stall", "WARNING") as logs:
            watch.tick(106.0, 1006.0)  # this thread itself woke 5.5 s late
        self.assertIn("WooSarr was paused for 5.5 s", logs.output[0])
        self.assertEqual([(s.kind, round(s.seconds, 1)) for s in watch.stalls], [("paused", 5.5)])
        with self.assertNoLogs("woosarr.stall", "WARNING"):
            watch.tick(106.5, 1006.5)  # the loop's silence was part of the pause, not a second stall

    def test_pause_says_what_the_hosts_cpus_did(self):
        from woosarr.services import stall_watch as sw
        samples = iter([(100, 0, 100, 800, 0, 0, 0, 0), (100, 0, 100, 800, 0, 0, 0, 0),
                        (900, 0, 200, 820, 80, 0, 0, 0)])
        watch = StallWatch(check=0.5, stall=3.0)
        with mock.patch.object(sw, "cpu_times", lambda: next(samples)):
            watch.tick(100.0, 1000.0)
            watch._beat = 100.0
            watch.tick(100.5, 1000.5)
            with self.assertLogs("woosarr.stall", "WARNING") as logs:
                watch.tick(108.0, 1008.0)
        # delta: user 800, system 100, idle 20, iowait 80 -> 900/1000 busy, 8% disk, 2% idle
        self.assertIn("were 90% busy, 8% waiting on disk, 0% taken by a hypervisor and 2% idle", logs.output[0])

    def test_cpu_access_pinning_and_quota(self):
        import tempfile
        from pathlib import Path

        from woosarr.services import stall_watch as sw
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            self.assertIsNone(sw.cpu_quota(root))  # no cgroup files: unknown = unlimited
            (root / "cpu.max").write_text("max 100000\n")
            self.assertIsNone(sw.cpu_quota(root))
            (root / "cpu.max").write_text("200000 100000\n")
            self.assertEqual(sw.cpu_quota(root), 2.0)
            (root / "cpu.max").unlink()
            (root / "cpu").mkdir()
            (root / "cpu" / "cpu.cfs_quota_us").write_text("150000")
            (root / "cpu" / "cpu.cfs_period_us").write_text("100000")
            self.assertEqual(sw.cpu_quota(root), 1.5)
        with mock.patch.object(sw.os, "cpu_count", lambda: 16), \
                mock.patch.object(sw.os, "sched_getaffinity", lambda _pid: {0, 1, 2, 3, 8}, create=True), \
                mock.patch.object(sw, "cpu_quota", lambda: 2.0):
            self.assertEqual(sw.cpu_access(), "may run on 5 of the host's 16 CPU cores (cores 0-3,8), "
                                              "limited to 2 cores' worth of time")
        with mock.patch.object(sw.os, "cpu_count", lambda: 8), \
                mock.patch.object(sw.os, "sched_getaffinity", lambda _pid: set(range(8)), create=True), \
                mock.patch.object(sw, "cpu_quota", lambda: None):
            self.assertEqual(sw.cpu_access(), "may run on 8 of the host's 8 CPU cores, no CPU time limit")
            watch = StallWatch(check=0.5, stall=3.0)
            watch.tick(100.0, 1000.0)
            watch._beat = 100.0
            with self.assertLogs("woosarr.stall", "WARNING") as logs:
                watch.tick(106.0, 1006.0)
            self.assertIn("WooSarr may run on 8 of the host's 8 CPU cores, no CPU time limit.", logs.output[0])

    def test_startup_logs_cpu_access(self):
        from tests.helpers import AppHarness
        with self.assertLogs("woosarr.app", "INFO") as logs:
            h = AppHarness()
        h.close()
        self.assertTrue(any("CPU access: WooSarr may run on" in line for line in logs.output))

    def test_during_finds_the_overlapping_stall(self):
        watch = StallWatch()
        watch.stalls.append(Stall("paused", 100.0, 114.0))
        self.assertEqual(watch.during(110.0, 120.0).kind, "paused")
        self.assertIsNone(watch.during(115.0, 120.0))


class BlameTests(unittest.TestCase):
    def test_late_command_during_a_stall_is_blamed_on_woosarr(self):
        discord_timing._too_late_dms.clear()
        it = FakeInteraction(417)
        it.user.send = mock.AsyncMock()
        it.created_at = datetime.now(UTC) - timedelta(seconds=7.4)

        async def gone(**kw):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10062, "message": "x"})

        it.response.defer = gone
        now = time.time()
        with mock.patch.object(stall_watch, "WATCH", StallWatch()):
            stall_watch.WATCH.stalls.append(Stall("paused", now - 6.5, now - 0.5))
            with self.assertLogs("woosarr.discord", "WARNING") as logs:
                asyncio.run(acknowledge(it, "/request", DiscordTiming()))
        self.assertIn("answered too late: WooSarr was paused for 6.0 s while it waited", logs.output[0])
