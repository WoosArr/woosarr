"""Helping unlinked members: the DM after a reaction, and link / invite buttons on reaction posts (P14-08)."""

import unittest
from datetime import timedelta
from types import SimpleNamespace
from unittest import mock

from tests.helpers import AppHarness
from tests.test_discord_features import (
    ALL,
    Base,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakeMessage,
    GuildMixin,
    perms,
)
from woosarr.core import app_settings
from woosarr.db.models import DiscordMember, ReactionPost, utcnow
from woosarr.dev.demo import MOVIE_NIGHT
from woosarr.services import link_help
from woosarr.services.discord_bot import _HelpView, _InviteModal


def settings(db, **changes):
    with db.session() as s:
        app_settings.update(s, changes)


def member_row(db, uid, roles=()):
    with db.session() as s:
        s.merge(DiscordMember(discord_user_id=uid, username=f"u{uid}", display_name=f"U{uid}",
                              role_ids=list(roles), in_guild=True))


class RulesTests(Base):
    """Member 501 is linked (Base); 503 isn't."""

    def test_dm_is_off_by_default(self):
        with self.db.session() as s:
            self.assertFalse(app_settings.load(s).reaction_link_dm)
            self.assertFalse(link_help.claim_dm(s, "503"))

    def test_dm_once_a_week_and_never_to_linked_members(self):
        settings(self.db, reaction_link_dm=True)
        now = utcnow()
        with self.db.session() as s:
            self.assertTrue(link_help.claim_dm(s, "503", now))
            self.assertFalse(link_help.claim_dm(s, "503", now + timedelta(minutes=1)))  # quick re-react
            self.assertFalse(link_help.claim_dm(s, "501", now))  # linked
            self.assertFalse(link_help.claim_dm(s, "503", now + timedelta(days=6)))
            self.assertTrue(link_help.claim_dm(s, "503", now + timedelta(days=8)))

    def test_who_may_invite_themselves(self):
        member_row(self.db, "503", ["777"])
        with self.db.session() as s:
            self.assertFalse(link_help.may_invite_self(s, "503"))  # nobody by default
            self.assertFalse(link_help.may_invite_self(s, "404"))  # not in the server
        settings(self.db, invite_role_ids=["777"])
        with self.db.session() as s:
            self.assertTrue(link_help.may_invite_self(s, "503"))
        settings(self.db, invite_role_ids=[], invite_all_members=True)
        with self.db.session() as s:
            self.assertTrue(link_help.may_invite_self(s, "503"))

    def test_dm_text(self):
        text = link_help.dm_text("Bragi", ["Movie *Night*"], can_invite=True)
        self.assertIn("**Movie \\*Night\\***", text)  # role names are escaped
        self.assertIn("in **Bragi**", text)
        self.assertIn(link_help.LINK_LABEL, text)
        self.assertIn(link_help.INVITE_LABEL, text)
        self.assertIn("Ask a server admin", link_help.dm_text(None, [], can_invite=False))
        self.assertNotIn(link_help.INVITE_LABEL, link_help.dm_text(None, [], can_invite=False))


class NamedGuild(FakeGuild):
    name = "Bragi"

    def get_role(self, rid):
        role = super().get_role(rid)
        return SimpleNamespace(id=rid, name=f"Role {rid}", position=role.position) if role else None


class ButtonMessage(FakeMessage):
    async def edit(self, content=None, allowed_mentions=None, **kw):
        await super().edit(content=content, allowed_mentions=allowed_mentions)
        if "view" in kw:
            self.view = kw["view"]


class ButtonChannel(FakeChannel):
    async def send(self, content, allowed_mentions=None, view=None):
        msg = ButtonMessage(8000 + len(self.sent))
        msg.content, msg.view = content, view
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg


def labels(view):
    return [(item.label, item.row, item.custom_id) for item in view.children] if view else []


class BotTests(GuildMixin, Base):
    def setUp(self):
        super().setUp()
        self.guild = NamedGuild(perms(**ALL), role_positions={8001: 1, 8002: 1, 8003: 1})
        self.guild.channel = ButtonChannel(900, "plex-access")
        self.guild.text_channels = [self.guild.channel]
        self.guild.members.append(FakeMember(503, []))
        self.attach(self.guild)
        self.dms = []

        async def send_dm(uid, text, what="", view=None):
            self.dms.append((uid, text, labels(view)))
            return True

        self.svc.send_dm = send_dm

    def test_unlinked_member_reacting_gets_one_dm_when_turned_on(self):
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual(self.dms, [])  # off by default
        settings(self.db, reaction_link_dm=True)
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=False))
        self.assertEqual(self.dms, [])  # removing a reaction never DMs
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=False))
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual(len(self.dms), 1)  # once a week
        uid, text, buttons = self.dms[0]
        self.assertEqual(uid, "503")
        self.assertIn("**Role 8001** in **Bragi**", text)
        self.assertIn("Ask a server admin", text)  # nobody may invite themselves by default
        self.assertEqual(buttons, [(link_help.LINK_LABEL, 0, "woosarr:help:link")])
        self.assertIn("8001", {str(r.id) for r in self.guild.get_member(503).roles})  # the role is still given

    def test_dm_offers_the_invite_button_to_members_allowed_to_invite_themselves(self):
        settings(self.db, reaction_link_dm=True, invite_all_members=True)
        member_row(self.db, "503")
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual([b[:2] for b in self.dms[0][2]], [(link_help.LINK_LABEL, 0), (link_help.INVITE_LABEL, 1)])

    def test_linked_members_get_no_dm(self):
        settings(self.db, reaction_link_dm=True)
        self.fire(self.svc.reaction_changed("7001", "501", "🎬", added=True))
        self.assertEqual(self.dms, [])

    def test_publish_puts_the_buttons_on_separate_rows_and_takes_them_off_again(self):
        self.guild.channel.messages[7001] = msg = ButtonMessage(7001)
        self.fire(self.svc.publish_post(1))
        self.assertFalse(hasattr(msg, "view"))  # a post without buttons is left as it was
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.link_button = post.invite_button = True
        self.fire(self.svc.publish_post(1))
        self.assertEqual([(lbl, row) for lbl, row, _ in labels(msg.view)],
                         [(link_help.LINK_LABEL, 0), (link_help.INVITE_LABEL, 1)])
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            self.assertEqual(post.buttons_posted, "link,invite")
            post.link_button = post.invite_button = False
        self.fire(self.svc.publish_post(1))
        self.assertIsNone(msg.view)  # taken off
        with self.db.session() as s:
            self.assertEqual(s.get(ReactionPost, 1).buttons_posted, "")

    def test_new_post_with_only_the_invite_button(self):
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.message_id, post.invite_button = None, True
        self.fire(self.svc.publish_post(1))
        self.assertEqual([lbl for lbl, _, _ in labels(self.guild.channel.sent[0].view)], [link_help.INVITE_LABEL])

    # ---- the invite button ------------------------------------------------------------

    def press_invite(self, uid, guild=True):
        response = SimpleNamespace(sent=[], modal=None)

        async def send_message(text, ephemeral=False):
            response.sent.append(text)

        async def send_modal(modal):
            response.modal = modal

        response.send_message, response.send_modal = send_message, send_modal
        interaction = SimpleNamespace(user=SimpleNamespace(id=uid), guild=self.guild if guild else None,
                                      response=response)
        self.fire(self.svc.handle_help_invite(interaction))
        return response

    def test_invite_button(self):
        r = self.press_invite(501)
        self.assertEqual((r.sent, r.modal), ([link_help.already_linked()], None))
        r = self.press_invite(503, guild=False)  # from the DM
        self.assertEqual((r.sent, r.modal), ([link_help.not_allowed_to_invite()], None))
        settings(self.db, invite_all_members=True)
        member_row(self.db, "503")
        r = self.press_invite(503)
        self.assertEqual(r.sent, [])
        self.assertIsInstance(r.modal, _InviteModal)

    def test_link_button(self):
        async def ok(*_a, **_k):
            return True

        def press(uid):
            sent = []

            async def send(text, view=None, ephemeral=False):
                sent.append((text, view))

            interaction = SimpleNamespace(user=SimpleNamespace(id=uid), guild=None,
                                          followup=SimpleNamespace(send=send))
            with mock.patch("woosarr.services.discord_bot.acknowledge", ok):
                self.fire(self.svc.handle_help_link(interaction))
            return sent

        self.assertEqual(press(501), [(link_help.already_linked(), None)])
        (text, view), = press(503)
        self.assertIn("Sign in with Plex", text)  # the same choices as /link
        self.assertIn("Sign in with Plex (verified)", [b.label for b in view.children])

    def test_the_buttons_are_persistent(self):
        async def build():
            return _HelpView(self.svc)

        view = self.fire(build())
        self.assertIsNone(view.timeout)
        self.assertTrue(view.is_persistent())
        self.assertEqual([(lbl, row) for lbl, row, _ in labels(view)],
                         [(link_help.LINK_LABEL, 0), (link_help.INVITE_LABEL, 1)])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def test_buttons_are_off_by_default_and_saved(self):
        body = {"title": "Pick", "content": "React", "options": [{"emoji": "🎬", "role_id": MOVIE_NIGHT}]}
        out = self.c.post("/api/v1/reaction-posts", json=body).json()
        self.assertEqual((out["link_button"], out["invite_button"]), (False, False))
        pid = out["id"]
        out = self.c.put(f"/api/v1/reaction-posts/{pid}", json=dict(body, link_button=True)).json()
        self.assertEqual((out["link_button"], out["invite_button"]), (True, False))
        self.assertFalse(out["outdated"])  # not posted yet
        with self.h.app.state.db.session() as s:
            post = s.get(ReactionPost, pid)
            post.message_id, post.rendered_content = "7777", out["preview"]
        out = self.c.get("/api/v1/reaction-posts").json()[0]
        self.assertTrue(out["outdated"])  # the posted message doesn't have the button yet
        with self.h.app.state.db.session() as s:
            s.get(ReactionPost, pid).buttons_posted = "link"
        self.assertFalse(self.c.get("/api/v1/reaction-posts").json()[0]["outdated"])

    def test_setting(self):
        self.assertFalse(self.c.get("/api/v1/settings").json()["reaction_link_dm"])
        self.assertTrue(self.c.patch("/api/v1/settings", json={"reaction_link_dm": True}).json()["reaction_link_dm"])


if __name__ == "__main__":
    unittest.main()

