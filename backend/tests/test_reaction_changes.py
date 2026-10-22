"""Changing a published reaction post's emoji: old reactions kept working, or cleared."""

import unittest
from types import SimpleNamespace

from sqlalchemy import select

from tests.helpers import AppHarness
from tests.test_discord_features import (
    ALL,
    Base,
    FakeGuild,
    FakeMember,
    FakeMessage,
    FakeReaction,
    GuildMixin,
    perms,
)
from woosarr.db.models import Reaction, ReactionPost
from woosarr.dev.demo import ALICE, BOB, MOVIE_NIGHT, SERIES_FANS


class TrackedMessage(FakeMessage):
    async def remove_reaction(self, emoji, member):
        self.removed = getattr(self, "removed", []) + [str(emoji)]

    async def clear_reaction(self, emoji):
        self.cleared = getattr(self, "cleared", []) + [str(emoji)]


def user(uid, bot=False):
    return SimpleNamespace(id=uid, bot=bot)


class BotTests(GuildMixin, Base):
    """Post 1 now shows 🎥 for role 8001 (was 🎬); 📺 and 🍿 were removed."""

    def setUp(self):
        super().setUp()
        with self.db.session() as s:
            s.get(ReactionPost, 1).options = [{"emoji": "🎥", "role_id": "8001", "former": ["🎬"]}]
        self.guild = FakeGuild(perms(**ALL), role_positions={8001: 1, 8002: 1, 8003: 1})
        self.guild.members.append(FakeMember(503, []))
        self.attach(self.guild)

    def message(self, reactions):
        msg = TrackedMessage(7001, reactions)
        self.guild.channel.messages[7001] = msg
        return msg

    def roles_of(self, uid):
        return {str(r.id) for r in self.guild.get_member(uid).roles}

    def test_reacting_with_the_former_emoji_still_gives_the_role(self):
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=True))
        self.assertIn("8001", self.roles_of(503))
        self.fire(self.svc.reaction_changed("7001", "503", "🎬", added=False))
        self.assertNotIn("8001", self.roles_of(503))

    def test_publish_keeping_old_reactions_only_takes_the_bots_own_off(self):
        bot = user(999, bot=True)
        msg = self.message([FakeReaction("🎬", [user(503), bot]), FakeReaction("📺", [user(502), bot])])
        out = self.fire(self.svc.publish_post(1))
        self.assertEqual((msg.added, sorted(msg.removed), getattr(msg, "cleared", [])), (["🎥"], ["🎬", "📺"], []))
        self.assertEqual(out["notes"], [])
        self.assertIn("8001", self.roles_of(503))  # 🎬 still counts
        with self.db.session() as s:
            self.assertEqual(s.get(ReactionPost, 1).options[0]["former"], ["🎬"])  # still in use

    def test_former_emoji_dropped_once_nobody_uses_it(self):
        self.message([FakeReaction("🎥", [user(503)])])
        self.fire(self.svc.reconcile_reactions())
        with self.db.session() as s:
            self.assertEqual(s.get(ReactionPost, 1).options, [{"emoji": "🎥", "role_id": "8001"}])

    def test_publish_clearing_old_reactions(self):
        self.guild.me.guild_permissions = perms(**ALL, manage_messages=True)
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.options, post.clear_old_reactions = [{"emoji": "🎥", "role_id": "8001"}], True
            post.retired_options = [{"emoji": "🎬", "role_id": "8001"}]
            s.add(Reaction(post_id=1, discord_user_id="503", emoji="🎬"))
        self.guild.get_member(503).roles.append(SimpleNamespace(id=8001))
        msg = self.message([FakeReaction("🎬", [user(503)]), FakeReaction("🎥", [user(999, bot=True)])])
        out = self.fire(self.svc.publish_post(1))
        self.assertEqual((msg.cleared, out["notes"]), (["🎬"], []))
        self.assertNotIn("8001", self.roles_of(503))  # must react with 🎥 now
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            self.assertEqual((post.clear_old_reactions, post.retired_options), (False, []))

    def test_publish_removes_roles_from_a_removed_reaction_but_leaves_the_reactions(self):
        # Keeping old reactions: 📺's role went with its reaction; members' 📺 reactions stay.
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.retired_options = [{"emoji": "📺", "role_id": "8002"}]
            s.add(Reaction(post_id=1, discord_user_id="503", emoji="📺"))
        self.guild.get_member(503).roles.append(SimpleNamespace(id=8002))
        msg = self.message([FakeReaction("📺", [user(503), user(999, bot=True)])])
        self.fire(self.svc.publish_post(1))
        self.assertEqual((msg.removed, getattr(msg, "cleared", [])), (["📺"], []))
        self.assertNotIn("8002", self.roles_of(503))

    def test_clearing_without_manage_messages_says_so(self):
        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.options, post.clear_old_reactions = [{"emoji": "🎥", "role_id": "8001"}], True
        msg = self.message([FakeReaction("🎬", [user(503), user(999, bot=True)])])
        out = self.fire(self.svc.publish_post(1))
        self.assertEqual((getattr(msg, "cleared", []), msg.removed), ([], ["🎬"]))
        self.assertIn("Manage Messages", out["notes"][0])


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()
        self.h.app.state.sync.run_now(None, "test")
        self.body = {"title": "Pick", "content": "React", "channel_id": "123456789",
                     "options": [{"emoji": "🎬", "role_id": MOVIE_NIGHT}, {"emoji": "📺", "role_id": SERIES_FANS}]}
        r = self.c.post("/api/v1/reaction-posts", json=self.body)
        self.pid = r.json()["id"]
        self.c.put(f"/api/v1/roles/{MOVIE_NIGHT}/libraries", json={"library_ids": ["1"]})
        self.c.put("/api/v1/users/1001/link", json={"discord_user_id": ALICE})
        with self.h.app.state.db.session() as s:
            s.get(ReactionPost, self.pid).message_id = "555"  # published
            s.add_all([Reaction(post_id=self.pid, discord_user_id=ALICE, emoji="🎬"),
                       Reaction(post_id=self.pid, discord_user_id=BOB, emoji="📺")])

    def tearDown(self):
        self.h.close()

    def changed(self, **kw):
        return dict(self.body, options=[{"emoji": "🍿", "role_id": MOVIE_NIGHT}], **kw)

    def test_preview_of_what_members_who_reacted_get(self):
        r = self.c.post(f"/api/v1/reaction-posts/{self.pid}/reaction-changes", json=self.changed())
        self.assertEqual(r.status_code, 200, r.text)
        out = r.json()
        got = {c["emoji"]: (c["new_emoji"], c["role_name"], c["names"]) for c in out["changes"]}
        self.assertEqual(got, {"🎬": ("🍿", "Movie Night", ["Alice"]), "📺": (None, "Series Fans", ["Bob"])})
        # Keeping: only Bob loses (his role's reaction is gone). Clearing: Alice too, and her
        # Plex account loses Movies.
        self.assertEqual((out["keep_losing"], out["clear_losing"]), (1, 2))
        self.assertEqual(out["keep_impact"], [])
        self.assertEqual([(i["username"], i["lose"]) for i in out["clear_impact"]], [("alice", ["1"])])
        same = self.c.post(f"/api/v1/reaction-posts/{self.pid}/reaction-changes", json=self.body).json()
        self.assertEqual(same["changes"], [])
        moved = self.c.post(f"/api/v1/reaction-posts/{self.pid}/reaction-changes",
                            json=dict(self.body, channel_id="987654321")).json()
        self.assertTrue(moved["moved"])
        self.assertEqual(len(moved["changes"]), 2)

    def test_saving_keeps_old_emoji_or_marks_them_for_clearing(self):
        out = self.c.put(f"/api/v1/reaction-posts/{self.pid}", json=self.changed()).json()
        self.assertEqual((out["options"][0]["former"], out["clear_old_reactions"]), (["🎬"], False))
        # Swapping back: 🎬 is the emoji again, 🍿 becomes the former one.
        out = self.c.put(f"/api/v1/reaction-posts/{self.pid}", json=self.body).json()
        self.assertEqual(out["options"][0]["former"], ["🍿"])
        out = self.c.put(f"/api/v1/reaction-posts/{self.pid}", json=self.changed(keep_old_reactions=False)).json()
        self.assertEqual((out["options"][0]["former"], out["clear_old_reactions"]), ([], True))
        retired = self.c.get("/api/v1/reaction-posts").json()[0]
        with self.h.app.state.db.session() as s:
            self.assertEqual({r["emoji"] for r in s.get(ReactionPost, self.pid).retired_options}, {"🎬", "📺"})
        self.assertTrue(retired["clear_old_reactions"])
        # Anything sent as "former" is ignored.
        sneaky = dict(self.body, options=[{"emoji": "🎬", "role_id": MOVIE_NIGHT, "former": ["📺"]}],
                      keep_old_reactions=False)
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{self.pid}", json=sneaky).json()["options"][0]["former"], [])
        with self.h.app.state.db.session() as s:
            entry = s.scalars(select(Reaction)).all()
        self.assertEqual(len(entry), 2)  # nothing in Discord or the reaction cache changes until publish
