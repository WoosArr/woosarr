"""The Discord bot's custom status: a setting (default "Try /help for help"), set on connect and on change."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

import discord
from cryptography.fernet import Fernet

from tests.helpers import AppHarness
from woosarr.core import app_settings
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.services.discord_bot import DiscordService


class BotStatusTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.svc = DiscordService(self.h.app.state.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda *a, **k: None, plex=None)
        self.client = SimpleNamespace(change_presence=mock.AsyncMock())
        self.svc._client = self.client

    def tearDown(self):
        self.h.close()

    def shown(self):
        asyncio.run(self.svc.apply_presence())
        activity = self.client.change_presence.await_args.kwargs["activity"]
        return activity.name if activity else None

    def test_default_then_edited_then_none(self):
        self.assertEqual(self.c.get("/api/v1/settings").json()["bot_status"], "Try /help for help")
        self.assertEqual(self.shown(), "Try /help for help")
        self.assertIsInstance(self.client.change_presence.await_args.kwargs["activity"], discord.CustomActivity)
        r = self.c.patch("/api/v1/settings", json={"bot_status": "  Movie night\nFriday  "})
        self.assertEqual(r.json()["bot_status"], "Movie night Friday")  # one line
        self.assertEqual(self.shown(), "Movie night Friday")
        self.c.patch("/api/v1/settings", json={"bot_status": ""})
        self.assertIsNone(self.shown())
        self.assertEqual(self.c.patch("/api/v1/settings", json={"bot_status": "x" * 129}).status_code, 422)

    def test_saving_updates_the_bot_right_away(self):
        self.h.app.state.discord = mock.Mock()
        self.c.patch("/api/v1/settings", json={"bot_status": "Ask for /request"})
        self.h.app.state.discord.schedule_presence.assert_called_once()
        self.h.app.state.discord.reset_mock()
        self.c.patch("/api/v1/settings", json={"dry_run": True})
        self.h.app.state.discord.schedule_presence.assert_not_called()

    def test_stored_settings_without_it_get_the_default(self):
        with self.h.app.state.db.session() as s:
            self.assertEqual(app_settings.load(s).bot_status, "Try /help for help")
