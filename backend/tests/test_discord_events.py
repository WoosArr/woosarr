"""Simulated Discord gateway events through the real bot handlers (P11-02)."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import discord
from cryptography.fernet import Fernet
from sqlalchemy import select

from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import DiscordLink, DiscordMember, DiscordRole, PlexUser
from woosarr.db.session import Database, make_engine, migrate
from woosarr.services.discord_bot import DiscordService, WooSarrBot

GUILD = 709961910967206019


def role(rid, name="r", default=False, guild=GUILD):
    return SimpleNamespace(id=rid, name=name, color=SimpleNamespace(value=0x9B59B6), position=1, managed=False,
                           guild=SimpleNamespace(id=guild), is_default=lambda: default)


EVERYONE = role(GUILD, "@everyone", default=True)


def member(uid, roles, guild=GUILD, name="alice", bot=False):
    return SimpleNamespace(id=uid, name=name, display_name=name.title(), roles=[EVERYONE, *roles],
                           guild=SimpleNamespace(id=guild), bot=bot)


class DiscordEventTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        with self.db.session() as s:
            s.add(PlexUser(plex_user_id="11", username="alice"))
            s.flush()
            s.add(DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True, linked_by="self"))
        self.triggers = []
        self.service = DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())),
                                      lambda ids, reason, **kw: self.triggers.append((ids, reason)), plex=None)
        self.bot = WooSarrBot(self.service, GUILD)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def fire(self, coro):
        return asyncio.run(coro)

    def roles_of(self, uid):
        with self.db.session() as s:
            m = s.get(DiscordMember, uid)
            return (m.role_ids, m.in_guild) if m else None

    def test_role_added_to_linked_member_triggers_their_sync(self):
        r1, r2 = role(9001, "Movie Night"), role(9002, "Series")
        self.fire(self.bot.on_member_join(member(501, [r1])))
        self.triggers.clear()
        self.fire(self.bot.on_member_update(member(501, [r1]), member(501, [r1, r2])))
        self.assertEqual(self.triggers, [(["11"], "discord roles changed for Alice")])
        self.assertEqual(self.roles_of("501"), (["9001", "9002"], True))  # @everyone not stored

    def test_unrelated_update_and_other_guilds_ignored(self):
        r1 = role(9001)
        self.fire(self.bot.on_member_join(member(501, [r1])))
        self.triggers.clear()
        self.fire(self.bot.on_member_update(member(501, [r1]), member(501, [r1])))  # nothing changed
        self.fire(self.bot.on_member_update(member(501, [], guild=1), member(501, [r1], guild=1)))
        self.fire(self.bot.on_member_update(member(777, []), member(777, [r1], name="nolink")))  # not linked
        self.assertEqual(self.triggers, [])

    def test_member_leaves(self):
        self.fire(self.bot.on_member_join(member(501, [role(9001)])))
        self.triggers.clear()
        self.fire(self.bot.on_raw_member_remove(SimpleNamespace(guild_id=GUILD, user=SimpleNamespace(id=501, name="alice"))))
        self.assertEqual(self.triggers, [(["11"], "discord member left: alice")])
        self.assertFalse(self.roles_of("501")[1])

    def test_role_lifecycle(self):
        r1 = role(9001, "Movie Night")
        self.fire(self.bot.on_guild_role_create(r1))
        self.fire(self.bot.on_member_join(member(501, [r1])))
        self.triggers.clear()
        renamed = role(9001, "Film Club")
        self.fire(self.bot.on_guild_role_update(r1, renamed))
        with self.db.session() as s:
            self.assertEqual(s.get(DiscordRole, "9001").name, "Film Club")
        self.fire(self.bot.on_guild_role_delete(renamed))
        self.assertEqual(self.triggers, [(["11"], "discord role Film Club deleted")])
        with self.db.session() as s:
            self.assertTrue(s.get(DiscordRole, "9001").deleted)
            self.assertEqual(s.scalars(select(DiscordMember.role_ids)).one(), [])

    def test_bots_are_marked_in_the_cache(self):
        self.fire(self.bot.on_member_join(member(601, [], name="musicbot", bot=True)))
        with self.db.session() as s:
            self.assertTrue(s.get(DiscordMember, "601").is_bot)

    def test_link_command_is_registered_for_the_guild(self):
        cmds = self.bot.tree.get_commands(guild=SimpleNamespace(id=GUILD))
        self.assertEqual(sorted(c.name for c in cmds), ["help", "invite", "link", "unlink"])

    def test_help_lists_every_command_privately(self):
        cmd = self.bot.tree.get_command("help", guild=SimpleNamespace(id=GUILD))
        interaction = SimpleNamespace(response=SimpleNamespace(send_message=mock.AsyncMock()))
        self.bot.request_name = "plexrequest"
        asyncio.run(cmd.callback(interaction))
        text = interaction.response.send_message.await_args.args[0]
        self.assertTrue(interaction.response.send_message.await_args.kwargs["ephemeral"])
        for name in ("/link", "/unlink", "/invite email [member]", "/plexrequest title", "/help"):
            self.assertIn(f"`{name}", text)
        self.bot.request_name = None  # Seerr not set up: no request command to describe
        asyncio.run(cmd.callback(interaction))
        self.assertNotIn("title`", interaction.response.send_message.await_args.args[0])


class LiveLogFixTests(unittest.TestCase):
    """Fixes for errors seen in the live logs (2026-10-18)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.service = DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())), lambda *a, **k: None,
                                      plex=None)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_plex_sign_in_button_answers_discord_before_calling_plex(self):
        order = []

        class Plex:
            def start_login(self, *_a):
                order.append("plex")
                return "pin1", "https://app.plex.tv/auth#x"

        async def defer(**kw):
            order.append(("defer", kw.get("thinking")))

        async def followup(content=None, **kw):
            order.append(("followup", content[:12]))

        self.service._plex = Plex()
        it = SimpleNamespace(user=SimpleNamespace(id=501), response=SimpleNamespace(defer=defer),
                             followup=SimpleNamespace(send=followup))

        async def run():
            with mock.patch.object(self.service, "_finish_oauth_link", mock.AsyncMock()):
                await self.service.start_oauth_link(it)
                await asyncio.sleep(0)

        asyncio.run(run())
        self.assertEqual(order, [("defer", True), "plex", ("followup", "Sign in on t")])
        self.assertEqual(self.service._link_tasks, set())  # finished task released

    def test_short_disconnects_are_info_long_ones_warn(self):
        bot = WooSarrBot(self.service, GUILD)

        async def run(resume):
            await bot.on_connect()
            await bot.on_disconnect()
            if resume:
                await bot.on_resumed()
            await asyncio.sleep(0.05)

        with mock.patch("woosarr.services.discord_bot.DISCONNECT_GRACE_SECONDS", 0.01):
            with self.assertLogs("woosarr.discord", "INFO") as quick:
                asyncio.run(run(resume=True))
            self.assertFalse([r for r in quick.records if r.levelname == "WARNING"])
            bot._disconnect_watch = None
            with self.assertLogs("woosarr.discord", "WARNING") as long:
                asyncio.run(run(resume=False))
            self.assertIn("still disconnected", long.output[0])

    def test_revoked_token_gets_a_clear_message(self):
        client = SimpleNamespace(start=mock.AsyncMock(side_effect=discord.ConnectionClosed(
            SimpleNamespace(close_code=4004), shard_id=None)))
        asyncio.run(self.service._run(client, "token"))
        self.assertIn("Discord rejected the bot token", self.service._error)
