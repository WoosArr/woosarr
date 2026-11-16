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
from woosarr.services import link_help_text as texts
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
        st = app_settings.AppSettings()
        text = link_help.dm_text(st, "Bragi", ["Movie *Night*"], can_invite=True)
        self.assertIn("**Movie \\*Night\\***", text)  # role names are escaped
        self.assertIn("in **Bragi**", text)
        self.assertIn(texts.DEFAULT_LINK_LABEL, text)
        self.assertIn(texts.DEFAULT_INVITE_LABEL, text)
        self.assertIn("Ask a server admin", link_help.dm_text(st, None, [], can_invite=False))
        self.assertNotIn(texts.DEFAULT_INVITE_LABEL, link_help.dm_text(st, None, [], can_invite=False))

    def test_dm_text_and_labels_are_editable(self):
        st = app_settings.AppSettings(reaction_link_dm_text="Hi from {server}: {roles}. Press {link_button}.",
                                      reaction_link_dm_invite_text="Or {invite_button}.",
                                      reaction_link_dm_closing_text="", link_button_label="Link *me*",
                                      invite_button_label="Invite me")
        self.assertEqual(link_help.dm_text(st, "Bragi", ["Movies"], can_invite=True),
                         "Hi from Bragi: **Movies**. Press Link \\*me\\*.\nOr Invite me.")  # empty closing left out
        with self.assertRaises(ValueError):
            app_settings.AppSettings(reaction_link_dm_text="Hi {member}")  # unknown placeholder
        with self.assertRaises(ValueError):
            app_settings.AppSettings(link_button_label="   ")
        with self.assertRaises(ValueError):
            app_settings.AppSettings(invite_button_style="blue")
        self.assertEqual(app_settings.AppSettings(link_button_label="  Link   me ").link_button_label, "Link me")

    def test_buttons_key_changes_with_labels_and_colours(self):
        post = SimpleNamespace(link_button=True, invite_button=False)
        st = app_settings.AppSettings()
        key = link_help.buttons_key(post, st)
        self.assertTrue(key.startswith("link:"))
        self.assertLessEqual(len(link_help.buttons_key(SimpleNamespace(link_button=True, invite_button=True), st)), 32)
        self.assertNotEqual(key, link_help.buttons_key(post, st.model_copy(update={"link_button_style": "danger"})))
        self.assertEqual(link_help.buttons_key(SimpleNamespace(link_button=False, invite_button=False), st), "")


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
        self.assertEqual(buttons, [(texts.DEFAULT_LINK_LABEL, 0, "woosarr:help:link")])
        self.assertIn("8001", {str(r.id) for r in self.guild.get_member(503).roles})  # the role is still given

    def test_dm_offers_the_invite_button_to_members_allowed_to_invite_themselves(self):
        settings(self.db, reaction_link_dm=True, invite_all_members=True)
        member_row(self.db, "503")
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual([b[:2] for b in self.dms[0][2]], [(texts.DEFAULT_LINK_LABEL, 0), (texts.DEFAULT_INVITE_LABEL, 1)])

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
                         [(texts.DEFAULT_LINK_LABEL, 0), (texts.DEFAULT_INVITE_LABEL, 1)])
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            self.assertTrue(post.buttons_posted.startswith("link,invite:"))
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
        self.assertEqual([lbl for lbl, _, _ in labels(self.guild.channel.sent[0].view)], [texts.DEFAULT_INVITE_LABEL])

    def test_buttons_use_the_labels_and_colours_from_settings(self):
        settings(self.db, reaction_link_dm=True, invite_all_members=True, link_button_label="Link up",
                 link_button_style="danger", invite_button_label="Join", invite_button_style="secondary")
        member_row(self.db, "503")
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual([b[:2] for b in self.dms[0][2]], [("Link up", 0), ("Join", 1)])
        self.assertIn("Press **Link up**", self.dms[0][1])
        self.guild.channel.messages[7001] = msg = ButtonMessage(7001)
        with self.db.session() as s:
            s.get(ReactionPost, 1).link_button = True
        self.fire(self.svc.publish_post(1))
        (button,) = msg.view.children
        self.assertEqual((button.label, button.style.name), ("Link up", "danger"))

    def test_button_order(self):
        settings(self.db, reaction_link_dm=True, invite_all_members=True, button_order="invite_first")
        member_row(self.db, "503")
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertEqual([b[:2] for b in self.dms[0][2]],
                         [(texts.DEFAULT_INVITE_LABEL, 0), (texts.DEFAULT_LINK_LABEL, 1)])
        self.guild.channel.messages[7001] = msg = ButtonMessage(7001)
        with self.db.session() as s:
            s.get(ReactionPost, 1).link_button = True  # only one button: first row either way
        self.fire(self.svc.publish_post(1))
        self.assertEqual([(lbl, row) for lbl, row, _ in labels(msg.view)], [(texts.DEFAULT_LINK_LABEL, 0)])
        settings(self.db, button_order="link_first")  # a change reaches posted buttons
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.invite_button = True
        self.fire(self.svc.publish_post(1))
        self.assertEqual([lbl for lbl, _, _ in labels(msg.view)], [texts.DEFAULT_LINK_LABEL, texts.DEFAULT_INVITE_LABEL])
        settings(self.db, button_order="invite_first")
        self.assertEqual(self.fire(self.svc.refresh_posts()), 1)
        self.assertEqual([(lbl, row) for lbl, row, _ in labels(msg.view)],
                         [(texts.DEFAULT_INVITE_LABEL, 0), (texts.DEFAULT_LINK_LABEL, 1)])

    def test_new_labels_are_applied_to_posted_buttons(self):
        self.guild.channel.messages[7001] = msg = ButtonMessage(7001)
        with self.db.session() as s:
            s.get(ReactionPost, 1).link_button = True
        self.fire(self.svc.publish_post(1))
        msg.view = None
        self.assertEqual(self.fire(self.svc.refresh_posts()), 0)  # nothing changed
        settings(self.db, link_button_label="Link up", link_button_style="success")
        self.assertEqual(self.fire(self.svc.refresh_posts()), 1)
        (button,) = msg.view.children
        self.assertEqual((button.label, button.style.name), ("Link up", "success"))
        self.assertEqual(self.fire(self.svc.refresh_posts()), 0)  # remembered

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
        self.assertEqual((r.sent, r.modal), ([link_help.not_allowed_to_invite(app_settings.AppSettings())], None))
        settings(self.db, invite_all_members=True)
        member_row(self.db, "503")
        st = app_settings.AppSettings(invite_all_members=True)
        r = self.press_invite(503)  # no roles yet: nothing to invite to
        self.assertEqual((r.sent, r.modal), ([link_help.choose_libraries_first(st, in_dm=False)], None))
        self.assertIn("React above", r.sent[0])
        self.assertIn("on the reaction post", self.press_invite(503, guild=False).sent[0])
        member_row(self.db, "503", ["8001"])
        with mock.patch("woosarr.services.invites.libraries_for_member", return_value=frozenset({"1"})):
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
                         [(texts.DEFAULT_LINK_LABEL, 0), (texts.DEFAULT_INVITE_LABEL, 1)])


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
            post = s.get(ReactionPost, pid)
            post.buttons_posted = link_help.buttons_key(post, app_settings.load(s))
        self.assertFalse(self.c.get("/api/v1/reaction-posts").json()[0]["outdated"])

    def test_preview(self):
        r = self.c.post("/api/v1/settings/link-help/preview", json={"invite_button_label": "Join us"}).json()
        self.assertIn("**Join us**", r["invite"])
        self.assertNotIn("Join us", r["no_invite"])
        self.assertIn("Ask a server admin", r["no_invite"])
        self.assertEqual(r["styles"]["success"], "Green")
        self.assertEqual(r["defaults"]["link_button_label"], texts.DEFAULT_LINK_LABEL)
        self.assertEqual(self.c.post("/api/v1/settings/link-help/preview",
                                     json={"reaction_link_dm_text": "{nope}"}).status_code, 422)

    def test_setting(self):
        self.assertFalse(self.c.get("/api/v1/settings").json()["reaction_link_dm"])
        self.assertTrue(self.c.patch("/api/v1/settings", json={"reaction_link_dm": True}).json()["reaction_link_dm"])


if __name__ == "__main__":
    unittest.main()

