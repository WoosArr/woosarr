"""Hostile input: SQL fragments, path traversal, markup, null bytes and oversized values are
either refused (4xx) or handled as plain data; none may cause a 500 or reach a shell, the
filesystem or SQL as code."""

import unittest

import discord

from tests.helpers import AppHarness
from woosarr.db.models import DiscordMember

NASTY = ["' OR 1=1 --", "\"; DROP TABLE admin_users; --", "../../etc/passwd", "..%2F..%2Fapp.db",
         "<script>alert(1)</script>", "${7*7}{{7*7}}", "$(id)`id`", "a\x00b", "%", "_", "\\", "x" * 5000]


class HostileInputTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()
        with self.h.app.state.db.session() as s:
            s.add_all([DiscordMember(discord_user_id="900000000000000001", username="under_score"),
                       DiscordMember(discord_user_id="900000000000000002", username="underXscore")])

    def tearDown(self):
        self.h.close()

    def test_query_strings(self):
        for value in NASTY:
            for path, params in (("/api/v1/discord/members", {"q": value}), ("/api/v1/logs", {"q": value}),
                                 ("/api/v1/emoji/search", {"source": "emojigg", "q": value}),
                                 ("/api/v1/audit", {"q": value}), ("/api/v1/logs/file", {"name": value})):
                r = self.c.get(path, params=params)
                self.assertLess(r.status_code, 500, f"{path} {value[:30]!r}: {r.text[:200]}")
        self.assertEqual(self.c.get("/api/v1/logs/file", params={"name": "../app.db"}).status_code, 422)

    def test_path_parameters(self):
        for value in ("..%2F..%2Fapp.db", "%2e%2e%2fapp.db", "' OR 1=1", "<x>"):
            for path in (f"/api/v1/emoji/preview/emojigg/{value}", f"/api/v1/discord/emojis/{value}",
                         f"/api/v1/reaction-posts/{value}", f"/api/v1/users/{value}", f"/api/v1/errors/{value}/resolved"):
                r = self.c.get(path) if "preview" in path else self.c.delete(path) if "emojis" in path else self.c.get(path)
                self.assertLess(r.status_code, 500, f"{path}: {r.text[:200]}")
                self.assertNotEqual(r.status_code, 200, path)

    def test_bodies_are_validated(self):
        for value in NASTY:
            r = self.c.post("/api/v1/emoji/add", json={"source": "emojigg", "id": value, "name": value})
            self.assertEqual(r.status_code, 422, value[:30])
            r = self.c.post("/api/v1/reaction-posts", json={"title": value[:100], "content": value[:2000],
                                                            "options": [{"emoji": "🎬", "role_id": value}]})
            self.assertIn(r.status_code, (404, 422), value[:30])
        r = self.c.patch("/api/v1/settings", json={"request_command_name": "$(reboot)"})
        self.assertEqual(r.status_code, 422)

    def test_search_wildcards_are_literal(self):
        names = lambda q: [m["username"] for m in self.c.get("/api/v1/discord/members", params={"q": q}).json()]
        self.assertEqual(names("under_"), ["under_score"])  # not underXscore
        self.assertEqual(names("%"), [])

    def test_spa_paths_stay_in_the_static_folder(self):
        for path in ("/../../etc/passwd", "/..%2F..%2Fapp.db", "/assets/../../app.db"):
            r = self.c.get(path)
            self.assertNotIn("root:", r.text)
            self.assertLess(r.status_code, 500)


class BotMentionsTests(unittest.TestCase):
    def test_bot_never_pings_everyone_or_roles_by_default(self):
        from woosarr.services.discord_bot import WooSarrBot

        bot = WooSarrBot(service=None, guild_id=1)
        self.assertFalse(bot.allowed_mentions.everyone)
        self.assertFalse(bot.allowed_mentions.roles)
        self.assertIsInstance(bot.allowed_mentions, discord.AllowedMentions)
