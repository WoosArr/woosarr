"""Warnings and errors show names next to Discord and Plex ids."""

import logging
import unittest
from types import SimpleNamespace

from tests.helpers import AppHarness
from woosarr.core import names
from woosarr.db.models import DiscordMember, DiscordRole, PlexUser
from woosarr.services import error_digest, friendly_names

GUILD, MEMBER, ROLE, CHANNEL = "709961910967206019", "284362693538742273", "1561039587932119062", "1184392045718302722"


class FriendlyNameTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        with self.h.app.state.db.session() as s:
            s.add_all([DiscordMember(discord_user_id=MEMBER, username="rwoot", display_name="rWoot (715)"),
                       DiscordRole(role_id=ROLE, name="Movie Night"),
                       PlexUser(plex_user_id="183715749", username="rwoot_plex")])
        guild = SimpleNamespace(id=int(GUILD), name="Dave's Plex Chat",
                                channels=[SimpleNamespace(id=int(CHANNEL), name="plex-bot")])
        client = SimpleNamespace(is_ready=lambda: True, guild_id=int(GUILD), get_guild=lambda _g: guild,
                                 user=SimpleNamespace(id=42424242424242424242, name="WooSarr"))
        friendly_names.refresh(self.h.app.state.db, SimpleNamespace(_client=client))
        self.addCleanup(names.update, {}, {})

    def tearDown(self):
        self.h.close()

    def test_ids_in_a_warning_get_names(self):
        logging.getLogger("discord.app_commands.tree").error(
            "Ignoring exception in autocomplete for 'request' (Guild: %s, User: %s)", GUILD, MEMBER)
        logging.getLogger("woosarr.sync").warning("share read failed for user=183715749 in #%s, role %s",
                                                  CHANNEL, ROLE)
        logging.getLogger("woosarr.sync").info("sync for user=183715749")  # INFO: as it was
        lines = [e["message"] for e in self.c.get("/api/v1/logs", params={"limit": 50}).json()["entries"]]
        self.assertIn(f"(Guild: {GUILD} (server Dave's Plex Chat), User: {MEMBER} (@rWoot [715]))", lines[-3])
        self.assertEqual(lines[-2], f"share read failed for user=183715749 (Plex rwoot_plex) in "
                                    f"#{CHANNEL} (#plex-bot), role {ROLE} (role Movie Night)")
        self.assertEqual(lines[-1], "sync for user=183715749")

    def test_unknown_ids_and_other_numbers_stay_as_they_are(self):
        text = "port 31337 took 12.5 s, id 99999999999999999999, user=7, timestamp 1792642270.619467"
        self.assertEqual(names.annotate(text), text)

    def test_one_alert_whoever_it_happened_to(self):
        a = names.annotate(f"User: {MEMBER} failed")
        self.assertNotEqual(a, f"User: {MEMBER} failed")
        self.assertEqual(error_digest.normalize(a), error_digest.normalize("User: 123456789012345678 failed"))
