import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core import app_settings
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    PlexUser,
    Reaction,
    ReactionPost,
    RoleLibrary,
)
from woosarr.db.session import Database, make_engine, migrate
from woosarr.dev.demo import MODERATORS, MOVIE_NIGHT, SERIES_FANS
from woosarr.services import discord_features as F
from woosarr.services import invites
from woosarr.services.discord_bot import DiscordConfigError, DiscordService, normalize_emoji

GUILD = 709961910967206019


class PureFeatureTests(unittest.TestCase):
    def test_permission_values(self):
        self.assertEqual(F.permissions_value(["commands"]), 0)
        self.assertEqual(F.permissions_value(["link_roles", "create_roles"]), 1 << 28)
        v = F.permissions_value(["reaction_posts", "pin_posts", "create_channel"])
        for bit in (1 << 4, 1 << 6, 1 << 10, 1 << 11, 1 << 16, 1 << 28, 1 << 51):
            self.assertTrue(v & bit)  # reaction posts give roles, so Manage Roles too

    def test_feature_report(self):
        report = {f["key"]: f for f in F.feature_report({"view_channel": True, "send_messages": True}, False)}
        self.assertTrue(report["commands"]["available"])
        self.assertEqual(report["reaction_posts"]["missing"], ["Add Reactions", "Read Message History", "Manage Roles"])
        self.assertFalse(report["link_roles"]["available"])
        ok, why = F.available(list(report.values()), "link_roles")
        self.assertFalse(ok)
        self.assertIn("Manage Roles", why)
        self.assertIn("above", why)

    def test_desired_link_roles(self):
        members = {"a": frozenset({"x"}), "b": frozenset({"V", "L"}), "c": frozenset({"U"}), "d": frozenset()}
        links = {"a": True, "c": True}  # a verified, c verified now (was unverified), b unlinked, d unlinked
        changes = F.desired_link_roles(members, links, "V", "U", "L")
        self.assertEqual(changes["a"], ({"V", "L"}, set()))
        self.assertEqual(changes["b"], (set(), {"V", "L"}))
        self.assertEqual(changes["c"], ({"V", "L"}, {"U"}))
        self.assertNotIn("d", changes)
        self.assertEqual(F.desired_link_roles(members, links, None, None, None), {})

    def test_normalize_emoji(self):
        self.assertEqual(normalize_emoji(" 🎬 "), "🎬")
        self.assertEqual(normalize_emoji("<:plex:123456789>"), "<:plex:123456789>")


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="11", username="alice"), PlexUser(plex_user_id="12", username="bob"),
                       Library(library_id="1", title="Movies", type="movie"),
                       Library(library_id="2", title="TV", type="show"),
                       Library(library_id="3", title="Anime", type="show")])
            s.flush()
            s.add(DiscordLink(plex_user_id="11", discord_user_id="501", method="oauth", verified=True,
                              linked_by="self"))
            s.add(ReactionPost(id=1, title="Pick", content="React!", channel_id="900", message_id="7001",
                               options=[{"emoji": "🎬", "label": "Movies", "role_id": "8001"},
                                        # stored before one-role-per-reaction: still honoured
                                        {"emoji": "📺", "label": "TV", "role_ids": ["8002", "8003"]},
                                        {"emoji": "🍿", "label": "Snacks", "role_id": "8002"}]))
        self.triggers = []
        self.svc = DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda ids, reason, **kw: self.triggers.append((ids, reason)), plex=None)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()


# ---- fake guild -------------------------------------------------------------------


class FakeMember:
    def __init__(self, uid, roles=(), bot=False):
        self.id, self.bot = uid, bot
        self.roles = [SimpleNamespace(id=r) for r in roles]
        self.calls = []

    async def add_roles(self, *roles, reason=None):
        self.calls.append(("add", sorted(r.id for r in roles)))
        self.roles += [SimpleNamespace(id=r.id) for r in roles]

    async def remove_roles(self, *roles, reason=None):
        ids = {r.id for r in roles}
        self.calls.append(("remove", sorted(ids)))
        self.roles = [r for r in self.roles if r.id not in ids]


class FakeReaction:
    def __init__(self, emoji, users, me=True):
        self.emoji, self._users, self.me = emoji, users, me

    def users(self, limit=None):
        async def gen():
            for u in self._users:
                yield u
        return gen()


class FakeMessage:
    def __init__(self, mid, reactions=()):
        self.id, self.reactions, self.pinned, self.added, self.content = mid, list(reactions), False, [], ""
        self.jump_url = f"https://discord.com/channels/{GUILD}/900/{mid}"

    async def add_reaction(self, emoji):
        self.added.append(str(emoji))

    async def pin(self, reason=None):
        self.pinned = True

    async def edit(self, content=None, allowed_mentions=None):
        self.content = content
        self.edits = getattr(self, "edits", 0) + 1

    async def delete(self):
        self.deleted = True


class FakeChannel:
    def __init__(self, cid, name):
        self.id, self.name, self.category, self.messages, self.sent = cid, name, None, {}, []
        self.perms = {"view_channel": True, "send_messages": True, "add_reactions": True,
                      "read_message_history": True, "use_external_emojis": False, "administrator": False}

    def permissions_for(self, _me):
        return SimpleNamespace(**self.perms)

    async def send(self, content, allowed_mentions=None):
        msg = FakeMessage(8000 + len(self.sent))
        msg.content = content
        self.sent.append(msg)
        self.messages[msg.id] = msg
        return msg

    async def fetch_message(self, mid):
        import discord

        if mid not in self.messages:
            raise discord.NotFound(SimpleNamespace(status=404, reason="nf"), "Unknown Message")
        return self.messages[mid]


def discord_emoji(eid, name):
    import discord

    return discord.PartialEmoji(name=name, id=eid)


def perms(**granted):
    base = {n: False for n in F.PERMISSION_BITS}
    base.update(granted)
    return SimpleNamespace(administrator=False, **base)


class FakeGuild:
    def __init__(self, granted, top=10, role_positions=None):
        self.me = SimpleNamespace(guild_permissions=granted, top_role=SimpleNamespace(position=top))
        self._roles = role_positions or {}
        self.members = [FakeMember(501, ["555"]), FakeMember(502, ["V"]), FakeMember(999, [], bot=True)]
        self.channel = FakeChannel(900, "plex-access")
        self.text_channels = [self.channel]
        self.created_roles = []
        self.emojis = []

    def get_role(self, rid):
        return SimpleNamespace(position=self._roles[rid]) if rid in self._roles else None

    def get_member(self, uid):
        return next((m for m in self.members if m.id == uid), None)

    def get_channel(self, cid):
        return next((c for c in self.text_channels if c.id == cid), None)

    async def create_role(self, name, mentionable=False, reason=None, colour=None):
        role = SimpleNamespace(id=4242 + len(self.created_roles), name=name,
                               color=SimpleNamespace(value=colour.value if colour else 0), position=1, managed=False)
        self.created_roles.append(role)
        return role

    async def create_text_channel(self, name, reason=None):
        ch = FakeChannel(901, name)
        self.text_channels.append(ch)
        return ch


ALL = {"view_channel": True, "send_messages": True, "add_reactions": True, "read_message_history": True,
       "manage_roles": True}


class GuildMixin:
    """Fake connected guild for DiscordService (use with Base)."""

    def setUp(self):
        super().setUp()
        # The service checks isinstance(channel, discord.TextChannel); our fake stands in for it.
        patcher = mock.patch("discord.TextChannel", FakeChannel)
        patcher.start()
        self.addCleanup(patcher.stop)

    def attach(self, guild):
        self.svc._client = SimpleNamespace(is_ready=lambda: True, guild_id=GUILD, get_guild=lambda _g: guild)

    def fire(self, coro):
        return asyncio.run(coro)


class GuildFeatureTests(GuildMixin, Base):

    def test_link_roles_follow_links(self):
        guild = FakeGuild(perms(manage_roles=True), top=10, role_positions={555: 1, 556: 1, 557: 1})
        self.attach(guild)
        with self.db.session() as s:
            app_settings.update(s, {"verified_role_id": "55555", "linked_role_id": "55557"})
        guild._roles = {55555: 1, 55557: 1}
        result = self.fire(self.svc.sync_link_roles())
        self.assertEqual(result["added"], 2)  # 501 gets Verified + Linked
        self.assertEqual(guild.members[0].calls, [("add", [55555, 55557])])
        self.assertEqual(guild.members[2].calls, [])  # bots untouched

    def test_link_roles_need_manage_roles_and_hierarchy(self):
        guild = FakeGuild(perms(), role_positions={55555: 1})
        self.attach(guild)
        with self.db.session() as s:
            app_settings.update(s, {"verified_role_id": "55555"})
        self.assertEqual(self.fire(self.svc.sync_link_roles())["added"], 0)
        guild.me = SimpleNamespace(guild_permissions=perms(manage_roles=True), top_role=SimpleNamespace(position=1))
        guild._roles = {55555: 5}  # role above the bot
        status = {f["key"]: f for f in self.svc.feature_status()}
        self.assertFalse(status["link_roles"]["available"])
        self.assertTrue(status["link_roles"]["notes"])

    def test_create_role_with_colour_and_emoji_list(self):
        guild = FakeGuild(perms(manage_roles=True))
        guild.emojis = [discord_emoji(123456, "plex")]
        self.attach(guild)
        self.fire(self.svc.create_role("Movie Night", 0x9B59B6))
        self.assertEqual(guild.created_roles[0].color.value, 0x9B59B6)
        emojis = self.fire(self.svc.list_emojis())
        self.assertEqual(emojis[0]["text"], "<:plex:123456>")

    def test_create_role_requires_permission(self):
        guild = FakeGuild(perms())
        self.attach(guild)
        with self.assertRaises(DiscordConfigError):
            self.fire(self.svc.create_role("Plex · Movies"))
        guild.me.guild_permissions = perms(manage_roles=True)
        rid, name = self.fire(self.svc.create_role("Plex · Movies"))
        self.assertEqual(name, "Plex · Movies")
        with self.db.session() as s:
            self.assertEqual(s.get(DiscordRole, rid).name, "Plex · Movies")

    def test_publish_post_sends_reacts_pins(self):
        guild = FakeGuild(perms(**ALL, pin_messages=True))
        self.attach(guild)
        with self.db.session() as s:
            s.get(ReactionPost, 1).message_id = None
        out = self.fire(self.svc.publish_post(1))
        msg = guild.channel.sent[0]
        self.assertEqual((msg.added, msg.pinned, out["pinned"]), (["🎬", "📺", "🍿"], True, True))
        self.assertTrue(msg.content.startswith("React!\n\n🎬 <@&8001> — "))  # legend under the intro
        with self.db.session() as s:
            self.assertEqual(s.get(ReactionPost, 1).rendered_content, msg.content)
        with self.db.session() as s:
            self.assertEqual(s.get(ReactionPost, 1).message_id, str(msg.id))

    def test_publish_without_pin_permission_still_posts(self):
        guild = FakeGuild(perms(**ALL))
        self.attach(guild)
        with self.db.session() as s:
            s.get(ReactionPost, 1).message_id = None
        out = self.fire(self.svc.publish_post(1))
        self.assertFalse(out["pinned"])

    def test_publish_refused_up_front_when_channel_permissions_are_missing(self):
        guild = FakeGuild(perms(**ALL))
        self.attach(guild)
        with self.db.session() as s:
            s.get(ReactionPost, 1).message_id = None
        guild.channel.perms["add_reactions"] = False
        with self.assertRaisesRegex(DiscordConfigError, "missing Add Reactions in #plex-access"):
            self.fire(self.svc.publish_post(1))
        self.assertEqual(guild.channel.sent, [])  # nothing half-posted
        guild.channel.perms["add_reactions"] = True
        with self.db.session() as s:  # another server's custom emoji needs Use External Emojis
            post = s.get(ReactionPost, 1)
            post.options = [*post.options, {"emoji": "<:other:999888777>", "role_id": "8001"}]
        with self.assertRaisesRegex(DiscordConfigError, "Use External Emojis"):
            self.fire(self.svc.publish_post(1))
        guild.channel.perms["use_external_emojis"] = True
        self.fire(self.svc.publish_post(1))
        self.assertIn("<:other:999888777>", guild.channel.sent[0].added)

    def test_discord_refusal_while_publishing_is_a_clear_error(self):
        import discord

        guild = FakeGuild(perms(**ALL))
        self.attach(guild)
        with self.db.session() as s:
            s.get(ReactionPost, 1).message_id = None

        async def refuse(_emoji):
            raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"),
                                    {"code": 50013, "message": "Missing Permissions"})

        original = FakeMessage.add_reaction
        FakeMessage.add_reaction = lambda self, emoji: refuse(emoji)
        try:
            with self.assertRaisesRegex(DiscordConfigError, "Discord refused: Missing Permissions in #plex-access"):
                self.fire(self.svc.publish_post(1))
        finally:
            FakeMessage.add_reaction = original

    def test_publish_into_new_channel_needs_manage_channels(self):
        guild = FakeGuild(perms(**ALL))
        self.attach(guild)
        with self.assertRaises(DiscordConfigError):
            self.fire(self.svc.publish_post(1, "Plex Access"))
        guild.me.guild_permissions = perms(**ALL, manage_channels=True)
        out = self.fire(self.svc.publish_post(1, "Plex Access"))
        self.assertEqual(out["channel"], "plex-access")  # reused the existing channel with that name

    def roles_of(self, guild, uid):
        return {str(r.id) for r in guild.get_member(uid).roles}

    def test_reacting_gives_roles_and_unreacting_removes_them(self):
        guild = FakeGuild(perms(**ALL), top=10, role_positions={8001: 1, 8002: 1, 8003: 1})
        self.attach(guild)
        self.fire(self.svc.reaction_changed("7001", "501", "🎬", added=True))
        self.assertIn("8001", self.roles_of(guild, 501))
        self.fire(self.svc.reaction_changed("7001", "501", "🎬", added=False))
        self.assertNotIn("8001", self.roles_of(guild, 501))
        # Libraries come from the role mappings, so no direct access sync is queued.
        self.assertEqual(self.triggers, [])

    def test_role_kept_while_another_reaction_still_gives_it(self):
        guild = FakeGuild(perms(**ALL), role_positions={8002: 1, 8003: 1})
        self.attach(guild)
        self.fire(self.svc.reaction_changed("7001", "501", "📺", added=True))   # 8002 + 8003
        self.fire(self.svc.reaction_changed("7001", "501", "🍿", added=True))   # 8002
        self.fire(self.svc.reaction_changed("7001", "501", "📺", added=False))
        roles = self.roles_of(guild, 501)
        self.assertIn("8002", roles)      # still given by 🍿
        self.assertNotIn("8003", roles)

    def test_unlinked_members_get_roles_too(self):
        guild = FakeGuild(perms(**ALL), role_positions={8001: 1})
        self.attach(guild)
        self.fire(self.svc.reaction_changed("7001", "502", "🎬", added=True))
        self.assertIn("8001", self.roles_of(guild, 502))

    def test_ignored_reactions(self):
        guild = FakeGuild(perms(**ALL), role_positions={8001: 1})
        self.attach(guild)
        self.fire(self.svc.reaction_changed("7001", "501", "🙂", added=True))  # not an option
        self.fire(self.svc.reaction_changed("9999", "501", "🎬", added=True))  # not a post
        with self.db.session() as s:
            s.get(ReactionPost, 1).enabled = False
        self.fire(self.svc.reaction_changed("7001", "501", "🎬", added=True))  # disabled post
        self.assertNotIn("8001", self.roles_of(guild, 501))

    def test_without_manage_roles_reaction_is_recorded_only(self):
        guild = FakeGuild(perms(view_channel=True, send_messages=True, add_reactions=True, read_message_history=True))
        self.attach(guild)
        self.fire(self.svc.reaction_changed("7001", "501", "🎬", added=True))
        self.assertNotIn("8001", self.roles_of(guild, 501))
        with self.db.session() as s:
            self.assertEqual(len(s.scalars(select(Reaction)).all()), 1)
            self.assertIn("reaction_added", s.scalars(select(AuditLog.action)).all())

    def test_role_above_bot_blocks_feature(self):
        guild = FakeGuild(perms(**ALL), top=1, role_positions={8001: 5})
        self.attach(guild)
        status = {f["key"]: f for f in self.svc.feature_status()}
        self.assertFalse(status["reaction_posts"]["available"])
        self.assertTrue(status["reaction_posts"]["notes"])

    def test_reconcile_adds_and_removes_roles(self):
        guild = FakeGuild(perms(**ALL), role_positions={8001: 1, 8002: 1, 8003: 1})
        guild.members.append(FakeMember(777, [8002, 8003]))  # discord.py role ids are ints
        users = [SimpleNamespace(id=501, bot=False), SimpleNamespace(id=999, bot=True)]
        guild.channel.messages[7001] = FakeMessage(7001, [FakeReaction("🎬", users), FakeReaction("🙂", users)])
        self.attach(guild)
        with self.db.session() as s:
            s.add(Reaction(post_id=1, discord_user_id="777", emoji="📺"))  # reaction removed while offline
        self.fire(self.svc.reconcile_reactions())
        with self.db.session() as s:
            self.assertEqual({(r.discord_user_id, r.emoji) for r in s.scalars(select(Reaction))}, {("501", "🎬")})
        self.assertIn("8001", self.roles_of(guild, 501))
        self.assertFalse({"8002", "8003"} & self.roles_of(guild, 777))


class LegendTests(GuildMixin, Base):
    def setUp(self):
        super().setUp()
        with self.db.session() as s:
            s.add_all([DiscordRole(role_id="8001", name="Movie Night"), DiscordRole(role_id="8002", name="Snacks")])
            s.flush()
            s.add(RoleLibrary(role_id="8001", library_id="1"))
            s.add(RoleLibrary(role_id="8001", library_id="3"))

    def test_render(self):
        from woosarr.services import reaction_text as rt

        opts = [{"emoji": "🎬", "role_id": "8001"}, {"emoji": "🍿", "role_id": "8002", "text": "{emoji} snacks for {role_name}"}]
        text = rt.render("Hi", opts, legend=True, line_template="{emoji} {role} — {libraries}", header="**Pick:**",
                         role_names={"8001": "Movie Night", "8002": "Snacks"},
                         role_libraries={"8001": ["Anime", "Movies"]})
        self.assertEqual(text, "Hi\n\n**Pick:**\n🎬 <@&8001> — Anime, Movies\n🍿 snacks for Snacks")
        self.assertEqual(rt.render("Hi", opts, legend=False, line_template="x", header="", role_names={},
                                   role_libraries={}), "Hi")
        no_libs = rt.render("", opts[1:2], legend=True, line_template="{role_name}: {libraries}", header="",
                            role_names={"8002": "Snacks"}, role_libraries={})
        self.assertEqual(no_libs, "🍿 snacks for Snacks")
        self.assertEqual(rt.render_line("{role.__class__} {unknown} {emoji}", {"emoji": "🎬"}),
                         "{role.__class__} {unknown} 🎬")

    def test_post_text_updates_when_mapping_or_role_name_changes(self):
        guild = FakeGuild(perms(**ALL))
        guild.channel.messages[7001] = FakeMessage(7001)
        self.attach(guild)
        from woosarr.services import reaction_text as rt

        with self.db.session() as s:
            post = s.get(ReactionPost, 1)
            post.rendered_content = rt.render_post(s, post)  # as published
        self.assertEqual(self.fire(self.svc.refresh_posts()), 0)  # nothing changed: no edit
        with self.db.session() as s:
            s.add(RoleLibrary(role_id="8001", library_id="2"))  # Movie Night now also grants TV
        self.assertEqual(self.fire(self.svc.refresh_posts()), 1)
        msg = guild.channel.messages[7001]
        self.assertIn("Anime, Movies, TV", msg.content)
        with self.db.session() as s:
            s.get(ReactionPost, 1).options = [{"emoji": "🎬", "role_id": "8001", "text": "{emoji} {role_name}"}]
            s.get(DiscordRole, "8001").name = "Film Club"
        self.fire(self.svc.refresh_posts())
        self.assertIn("🎬 Film Club", msg.content)
        self.assertEqual(self.fire(self.svc.refresh_posts()), 0)


class InviteAllMembersTests(Base):
    def test_all_members_can_self_invite(self):
        from woosarr.plex_gateway import FakePlexGateway

        with self.db.session() as s:
            s.add(DiscordMember(discord_user_id="600", username="new", role_ids=["r1"]))
            s.add(RoleLibrary(role_id="r1", library_id="1"))
            app_settings.update(s, {"dry_run": False, "link_cooldown_seconds": 0})
        gw = FakePlexGateway()
        with self.db.session() as s:
            r = invites.create(s, gw, actor_discord_id="600", actor_is_admin=False, target_discord_id="600",
                               identifier="new@x.com")
        self.assertFalse(r.ok)
        with self.db.session() as s:
            app_settings.update(s, {"invite_all_members": True})
            r = invites.create(s, gw, actor_discord_id="600", actor_is_admin=False, target_discord_id="600",
                               identifier="new@x.com")
        self.assertTrue(r.ok, r.message)


class ReactionPostApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness(demo=True)
        self.c = self.h.setup_admin()
        self.h.app.state.sync.run_now(None, "test")

    def tearDown(self):
        self.h.close()

    def test_crud_and_validation(self):
        body = {"title": "Pick", "content": "React for access", "options": [
            {"emoji": "🎬", "label": "ignored", "role_id": MOVIE_NIGHT}, {"emoji": "📺", "role_id": SERIES_FANS}]}
        r = self.c.post("/api/v1/reaction-posts", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        pid = r.json()["id"]
        # The label is always the role's name.
        self.assertEqual([o["label"] for o in r.json()["options"]], ["Movie Night", "Series Fans"])
        with self.h.app.state.db.session() as s:
            s.get(DiscordRole, MOVIE_NIGHT).name = "Film Club"  # renamed in Discord
        self.assertEqual(self.c.get("/api/v1/reaction-posts").json()[0]["options"][0]["label"], "Film Club")
        two_roles = dict(body, options=[{"emoji": "🎬", "role_ids": [MOVIE_NIGHT, SERIES_FANS]}])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=two_roles).status_code, 422)
        dup = dict(body, options=[body["options"][0], body["options"][0]])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=dup).status_code, 422)
        bad = dict(body, options=[{"emoji": "🎬", "role_id": "99999"}])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=bad).status_code, 404)
        with self.h.app.state.db.session() as s:
            s.get(DiscordRole, MODERATORS).managed = True
        bot_role = dict(body, options=[{"emoji": "🎬", "role_id": MODERATORS}])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=bot_role).status_code, 422)
        no_roles = dict(body, options=[{"emoji": "🎬"}])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=no_roles).status_code, 422)
        many = dict(body, options=[{"emoji": f"<:e{i}:1000{i}>", "role_id": MOVIE_NIGHT} for i in range(21)])
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=many).status_code, 422)
        self.assertEqual(self.c.post(f"/api/v1/reaction-posts/{pid}/publish", json={}).status_code, 409)  # demo
        self.assertEqual(len(self.c.get("/api/v1/reaction-posts").json()), 1)
        pv = self.c.post("/api/v1/reaction-posts/preview", json=dict(body, legend_header="Pick one")).json()
        self.assertIn(f"Pick one\n🎬 <@&{MOVIE_NIGHT}> — ", pv["text"])
        self.assertEqual(pv["length"], len(pv["text"]))
        out = self.c.get("/api/v1/reaction-posts").json()[0]
        self.assertFalse(out["outdated"])  # never posted
        self.assertIn(f"<@&{SERIES_FANS}>", out["preview"])
        long = dict(body, content="x" * 1990)
        self.assertEqual(self.c.put(f"/api/v1/reaction-posts/{pid}", json=long).status_code, 422)
        self.assertEqual(self.c.delete(f"/api/v1/reaction-posts/{pid}").status_code, 204)
        self.assertEqual(self.c.get("/api/v1/reaction-posts").json(), [])

    def test_invite_url_and_features(self):
        r = self.c.get("/api/v1/discord/invite-url", params={"features": "link_roles,reaction_posts"}).json()
        self.assertEqual(r["permissions"], F.permissions_value(["link_roles", "reaction_posts"]))
        self.assertEqual(self.c.get("/api/v1/discord/invite-url", params={"features": "nope"}).status_code, 422)
        st = self.c.get("/api/v1/discord/status").json()
        self.assertEqual({f["key"] for f in st["features"]}, F.FEATURE_KEYS)

    def test_settings_roles_validated(self):
        self.assertEqual(self.c.patch("/api/v1/settings", json={"verified_role_id": "abc"}).status_code, 422)
        r = self.c.patch("/api/v1/settings", json={"verified_role_id": "123456", "invite_all_members": True})
        self.assertEqual(r.status_code, 200)


class DocsMatchCodeTests(unittest.TestCase):
    def test_documented_permission_integers(self):
        doc = (Path(__file__).resolve().parents[2] / "docs" / "discord-notes.md").read_text()
        self.assertIn(f"All features: `{F.permissions_value(F.FEATURE_KEYS)}`", doc)
        for name, bit in F.PERMISSION_BITS.items():
            self.assertIn(f"{F.PERMISSION_LABELS[name]} `{bit}`", doc, name)
