"""/link, /unlink and the username form answer Discord before any database work (3 s limit)."""

import asyncio
import unittest
from datetime import timedelta
from types import SimpleNamespace

import discord
from cryptography.fernet import Fernet

from tests.helpers import AppHarness
from tests.test_requests import FakeInteraction
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.services.discord_bot import DiscordService


class LinkCommandTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.svc = DiscordService(self.h.app.state.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda *a, **k: None, plex=None)

    def tearDown(self):
        self.h.close()

    def test_link_defers_first_then_offers_the_buttons(self):
        it = FakeInteraction(501)
        asyncio.run(self.svc.handle_link_command(it))
        self.assertEqual(it.log[0], ("defer", {"ephemeral": True, "thinking": True}))
        kind, text, kw = it.log[1]
        self.assertEqual(kind, "followup")
        self.assertIn("Sign in with Plex", text)
        self.assertTrue(kw["ephemeral"])
        self.assertIsNotNone(kw["view"])

    def test_unlink_when_not_linked(self):
        it = FakeInteraction(501)
        asyncio.run(self.svc.handle_unlink_command(it))
        self.assertEqual([e[0] for e in it.log], ["defer", "followup"])
        self.assertIn("isn't linked", it.log[1][1])

    def test_too_late_is_logged_and_the_member_is_told_by_dm(self):
        from unittest import mock

        from woosarr.services import discord_timing
        discord_timing._too_late_dms.clear()
        it = FakeInteraction(501)
        it.user.send = mock.AsyncMock()
        it.created_at = discord.utils.utcnow() - timedelta(seconds=4)

        async def gone(**kw):
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"),
                                   {"code": 10062, "message": "Unknown interaction"})

        it.response.defer = gone
        with self.assertLogs("woosarr.discord", "WARNING") as logs:
            asyncio.run(self.svc.handle_link_command(it))
        self.assertIn("/link from member 501 answered too late: Discord (or the network to it) delivered it late. "
                      "It reached WooSarr 4.", logs.output[0])
        self.assertEqual(it.log, [])  # nothing else was tried
        self.assertIn("try /link again", it.user.send.await_args.args[0])
        asyncio.run(self.svc.handle_link_command(it))
        self.assertEqual(it.user.send.await_count, 1)  # at most one DM a minute
