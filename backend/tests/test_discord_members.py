"""Users → Discord: member list, role-change impact preview and bulk role changes."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest import mock

from cryptography.fernet import Fernet
from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core.secret_store import SecretStore
from woosarr.core.secrets import SecretBox
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    LibraryState,
    PlexUser,
    RoleLibrary,
)
from woosarr.services.discord_bot import DiscordConfigError, DiscordService

GUILD = 100000
MOVIES, TV, HIGH, BOTROLE = "900001", "900002", "900009", "900007"


def seed(db):
    with db.session() as s:
        s.add_all([PlexUser(plex_user_id="11", username="alice"), PlexUser(plex_user_id="12", username="bob"),
                   Library(library_id="1", title="Movies", type="movie"), Library(library_id="2", title="TV", type="show"),
                   DiscordRole(role_id=MOVIES, name="Movie Night", position=3),
                   DiscordRole(role_id=TV, name="Series Fans", position=2),
                   DiscordMember(discord_user_id="500001", username="alice_d", display_name="Alice", role_ids=[MOVIES]),
                   DiscordMember(discord_user_id="500002", username="bobby", display_name="Bob", role_ids=[]),
                   DiscordMember(discord_user_id="500003", username="carl", display_name=None, role_ids=[TV]),
                   DiscordMember(discord_user_id="500004", username="gone", role_ids=[], in_guild=False)])
        s.flush()
        s.add_all([RoleLibrary(role_id=MOVIES, library_id="1"), RoleLibrary(role_id=TV, library_id="2"),
                   LibraryState(plex_user_id="11", library_id="1"),
                   DiscordLink(plex_user_id="11", discord_user_id="500001", method="oauth", verified=True,
                               linked_by="self"),
                   DiscordLink(plex_user_id="12", discord_user_id="500002", method="username", verified=False,
                               linked_by="self")])


class MemberApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        seed(self.h.app.state.db)

    def tearDown(self):
        self.h.close()

    def ids(self, link=None):
        r = self.c.get("/api/v1/discord/member-list", params={"link": link} if link else {})
        self.assertEqual(r.status_code, 200, r.text)
        return [m["discord_user_id"] for m in r.json()]

    def test_member_list_with_roles_links_and_filters(self):
        rows = {m["discord_user_id"]: m for m in self.c.get("/api/v1/discord/member-list").json()}
        self.assertEqual(sorted(rows), ["500001", "500002", "500003"])  # left the server: not listed
        alice = rows["500001"]
        self.assertEqual([r["name"] for r in alice["roles"]], ["Movie Night"])
        self.assertEqual((alice["link"]["plex_username"], alice["link"]["verified"]), ("alice", True))
        self.assertIsNone(rows["500003"]["link"])
        self.assertEqual(self.ids("verified"), ["500001"])
        self.assertEqual(self.ids("unverified"), ["500002"])
        self.assertEqual(sorted(self.ids("linked")), ["500001", "500002"])
        self.assertEqual(self.ids("unlinked"), ["500003"])

    def test_impact_preview_follows_role_mappings(self):
        r = self.c.post("/api/v1/discord/members/roles/preview",
                        json={"discord_user_ids": ["500001", "500002", "500003"], "add_role_ids": [TV],
                              "remove_role_ids": [MOVIES]})
        self.assertEqual(r.status_code, 200, r.text)
        got = {x["username"]: (x["gain"], x["lose"]) for x in r.json()}
        # alice: loses Movies, gains TV; bob (unverified, counted) gains TV; carl isn't linked.
        self.assertEqual(got, {"alice": (["2"], ["1"]), "bob": (["2"], [])})

    def test_bulk_change_needs_roles_and_is_admin_only(self):
        r = self.c.post("/api/v1/discord/members/roles", json={"discord_user_ids": ["500001"]})
        self.assertEqual(r.status_code, 422)
        short = self.c.post("/api/v1/discord/members/roles/preview",
                            json={"discord_user_ids": ["500001"], "add_role_ids": ["9003"]})
        self.assertEqual(short.status_code, 200, short.text)  # short ids (demo) are fine
        bad = self.c.post("/api/v1/discord/members/roles", json={"discord_user_ids": ["x"], "add_role_ids": [TV]})
        self.assertEqual(bad.status_code, 422)
        anon = self.h.new_client().post("/api/v1/discord/members/roles",
                                        json={"discord_user_ids": ["500001"], "add_role_ids": [TV]})
        self.assertEqual(anon.status_code, 401)


def fake_role(rid, name, position, managed=False, default=False):
    return SimpleNamespace(id=int(rid), name=name, position=position, managed=managed, is_default=lambda: default)


def fake_member(uid, roles, bot=False):
    m = SimpleNamespace(id=int(uid), name=f"user{uid}", display_name=f"User {uid}", bot=bot, roles=list(roles))
    m.add_roles = mock.AsyncMock()
    m.remove_roles = mock.AsyncMock()
    return m


class BulkRoleChangeTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.db = self.h.app.state.db
        seed(self.db)
        self.triggers = []
        self.svc = DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())),
                                  lambda ids, reason: self.triggers.append((ids, reason)), plex=None)
        everyone = fake_role(GUILD, "@everyone", 0, default=True)
        self.roles = {r.id: r for r in (everyone, fake_role(MOVIES, "Movie Night", 3), fake_role(TV, "Series Fans", 2),
                                        fake_role(HIGH, "Admins", 9), fake_role(BOTROLE, "Some Bot", 1, managed=True))}
        self.members = {
            500001: fake_member("500001", [everyone, self.roles[int(MOVIES)]]),
            500002: fake_member("500002", [everyone]),
            500005: fake_member("500005", [everyone], bot=True),
        }
        self.guild = SimpleNamespace(id=GUILD, me=SimpleNamespace(top_role=SimpleNamespace(position=5)),
                                     get_role=lambda rid: self.roles.get(rid), get_member=lambda mid: self.members.get(mid))

    def tearDown(self):
        self.h.close()

    def change(self, ids, add=(), remove=()):
        with mock.patch.object(self.svc, "_guild", return_value=self.guild), \
                mock.patch.object(self.svc, "require"):
            return asyncio.run(self.svc.change_member_roles(list(ids), list(add), list(remove), "admin:admin"))

    def test_adds_and_removes_only_what_differs_and_syncs_linked_accounts(self):
        out = {r["discord_user_id"]: r for r in self.change(["500001", "500002", "500005", "500099"], add=[TV],
                                                             remove=[MOVIES])}
        self.assertEqual((out["500001"]["added"], out["500001"]["removed"]), ([TV], [MOVIES]))
        self.assertEqual((out["500002"]["added"], out["500002"]["removed"]), ([TV], []))  # had no Movie Night
        self.assertEqual(out["500005"]["error"], "bots are skipped")
        self.assertEqual(out["500099"]["error"], "not in the server")
        self.members[500002].remove_roles.assert_not_awaited()
        with self.db.session() as s:
            self.assertEqual(s.get(DiscordMember, "500001").role_ids, [TV])  # cache updated right away
            entry = s.scalar(select(AuditLog).where(AuditLog.action == "discord_roles_changed_admin"))
        self.assertEqual((entry.actor, entry.details["add"], entry.details["remove"]), ("admin:admin", [TV], [MOVIES]))
        self.assertEqual(sorted(self.triggers[0][0]), ["11", "12"])

    def test_unusable_roles_refuse_everything(self):
        for add in ([HIGH], [BOTROLE], [str(GUILD)], ["900404"]):
            with self.assertRaises(DiscordConfigError, msg=add):
                self.change(["500001"], add=add)
        with self.assertRaises(DiscordConfigError):
            self.change(["500001"], add=[TV], remove=[TV])
        self.members[500001].add_roles.assert_not_awaited()

    def test_discord_refusal_is_reported_per_member(self):
        import discord
        self.members[500002].add_roles.side_effect = discord.Forbidden(SimpleNamespace(status=403, reason="x"), "no")
        out = {r["discord_user_id"]: r for r in self.change(["500001", "500002"], add=[TV])}
        self.assertIn("refused", out["500002"]["error"])
        self.assertEqual(out["500001"]["added"], [TV])
