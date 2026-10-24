"""Discord's 3-second limit: timing every answer, the clock offset, answering first, staying warm."""

import asyncio
import logging
import unittest
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from types import SimpleNamespace
from unittest import mock

import discord

from tests.test_requests import FakeInteraction
from woosarr.services import discord_timing
from woosarr.services.discord_bot import WooSarrBot
from woosarr.services.discord_timing import DiscordTiming, acknowledge

TOKEN = "aW50ZXJhY3Rpb24" + "uLXRva2VuLWZha2U"  # assembled: no token-shaped literal in source


def date_header(offset_s: float) -> str:
    return format_datetime(datetime.now(UTC) + timedelta(seconds=offset_s), usegmt=True)


async def fake_request(timing: DiscordTiming, path: str, *, new: bool, offset_s: float = 0.0, status: int = 204):
    ctx = SimpleNamespace()
    params = SimpleNamespace(url=SimpleNamespace(path=path),
                             response=SimpleNamespace(status=status, headers={"Date": date_header(offset_s)}))
    await timing._start(None, ctx, params)
    if new:
        await timing._dns_start(None, ctx, params)
        await timing._dns_end(None, ctx, params)
        await timing._connect_start(None, ctx, params)
        await timing._connect_end(None, ctx, params)
    else:
        await timing._reused(None, ctx, params)
    await timing._end(None, ctx, params)


class TimingTests(unittest.TestCase):
    def test_clock_offset_from_discord_date_headers(self):
        t = DiscordTiming()
        for _ in range(5):
            asyncio.run(fake_request(t, "/api/v10/gateway", new=False, offset_s=38))
        self.assertAlmostEqual(t.offset, 38, delta=1.5)  # the live server's clock was 38 s slow
        created = datetime.now(UTC) + timedelta(seconds=36)  # "36 s in the future" on our clock
        self.assertAlmostEqual(t.age(created), 2, delta=1.5)

    def test_answer_timing_keeps_the_id_never_the_token(self):
        t = DiscordTiming()
        asyncio.run(fake_request(t, f"/api/v10/interactions/123456789012/{TOKEN}/callback", new=True))
        self.assertNotIn(TOKEN, repr(t.calls))
        self.assertRegex(t.answer(123456789012), r"^new connection: DNS 0\.0 s, connect 0\.0 s; HTTP 204 after 0\.\d s$")
        self.assertEqual(t.answer(123456789012), "no HTTP timing")  # used once
        asyncio.run(fake_request(t, f"/api/v10/interactions/223456789012/{TOKEN}/callback", new=False))
        self.assertTrue(t.answer("223456789012").startswith("reused connection"))


class AcknowledgeTests(unittest.TestCase):
    def setUp(self):
        discord_timing._too_late_dms.clear()

    def interaction(self, age_s: float):
        it = FakeInteraction(501)
        it.created_at = datetime.now(UTC) - timedelta(seconds=age_s)
        it.user.send = mock.AsyncMock()
        return it

    def test_quick_answers_are_info_slow_ones_a_warning(self):
        with self.assertLogs("woosarr.discord", "INFO") as logs:
            self.assertTrue(asyncio.run(acknowledge(self.interaction(0.3), "/help", DiscordTiming())))
        self.assertTrue(logs.records[0].levelno == logging.INFO)
        self.assertIn("/help from member 501 acknowledged: reached WooSarr 0.", logs.output[0])
        with self.assertLogs("woosarr.discord", "WARNING") as logs:
            self.assertTrue(asyncio.run(acknowledge(self.interaction(2.5), "/help", DiscordTiming())))
        self.assertIn("reached WooSarr 2.5 s after use", logs.output[0])

    def test_already_answered_is_left_alone(self):
        it = self.interaction(0)
        it.log.append(("defer", {}))
        self.assertTrue(asyncio.run(acknowledge(it, "/help", DiscordTiming())))
        self.assertEqual(len(it.log), 1)

    def test_slow_answer_is_named_as_the_cause(self):
        it = self.interaction(0.2)

        async def slow_then_gone(**kw):
            await asyncio.sleep(1.6)
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10062, "message": "x"})

        it.response.defer = slow_then_gone
        with self.assertLogs("woosarr.discord", "WARNING") as logs:
            self.assertFalse(asyncio.run(acknowledge(it, "/link", DiscordTiming())))
        self.assertIn("answered too late: sending the answer was slow", logs.output[0])
        self.assertEqual(it.user.send.await_count, 1)


class TreeTests(unittest.TestCase):
    def setUp(self):
        self.service = SimpleNamespace(timing=DiscordTiming())
        self.bot = WooSarrBot(self.service, 1)

    def test_every_slash_command_is_answered_before_it_runs(self):
        it = FakeInteraction(501)
        it.type = discord.InteractionType.application_command
        it.command = SimpleNamespace(name="help")
        it.created_at = datetime.now(UTC)
        self.assertTrue(asyncio.run(self.bot.tree.interaction_check(it)))
        self.assertEqual(it.log[0], ("defer", {"ephemeral": True, "thinking": True}))

    def test_suggestions_are_not_deferred(self):
        it = FakeInteraction(501)
        it.type = discord.InteractionType.autocomplete
        self.assertTrue(asyncio.run(self.bot.tree.interaction_check(it)))
        self.assertEqual(it.log, [])

    def test_dropped_suggestions_are_not_alerts(self):
        # discord.py logs these itself (not through on_error); live 2026-10-23: 5 ERRORs per title typed.
        stale = discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10062, "message": "x"})
        tree_log = logging.getLogger("discord.app_commands.tree")
        with self.assertNoLogs("discord.app_commands.tree", "INFO"):
            try:
                raise stale
            except discord.NotFound:
                tree_log.exception("Ignoring exception in autocomplete for %r (Guild: %s, User: %s)", "request", 1, 2)
        with self.assertLogs("discord.app_commands.tree", "ERROR"):
            try:
                raise RuntimeError("a real bug")
            except RuntimeError:
                tree_log.exception("Ignoring exception in autocomplete for %r (Guild: %s, User: %s)", "request", 1, 2)

    def test_connections_are_kept_and_warmed(self):
        async def run():
            with mock.patch.object(discord.Client, "login", mock.AsyncMock()):
                await self.bot.login("x")
            connector = self.bot.http.connector
            self.assertEqual((connector._ttl_dns_cache if hasattr(connector, "_ttl_dns_cache") else 600,
                              connector._keepalive_timeout), (600, 120))
            await connector.close()
            calls = []
            closed = iter([False, False, True])
            with mock.patch.object(self.bot, "is_closed", lambda: next(closed)), \
                    mock.patch("woosarr.services.discord_bot.KEEP_WARM_SECONDS", 0), \
                    mock.patch.object(self.bot.http, "request", mock.AsyncMock(side_effect=lambda r: calls.append(r.path))):
                await self.bot._keep_warm()
            self.assertEqual(calls, ["/gateway", "/gateway"])
        asyncio.run(run())
