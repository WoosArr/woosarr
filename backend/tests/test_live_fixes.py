"""Regressions from the live server (2026-10-17):

1. The owner running /link got "doesn't have access to this Plex server".
2. The write check failed for a user with no libraries ("not a friend").
3. Friends without library access were missing from the user list.
Plus: migration 0001 -> 0002 on an existing database.
"""

import tempfile
import unittest
from pathlib import Path

from alembic import command
from sqlalchemy import inspect, select, text

from tests.fake_plextv import MACHINE
from tests.helpers import AppHarness
from tests.test_integrations import OWNER, Base
from woosarr.core import app_settings
from woosarr.db.models import DiscordLink, DiscordMember, PlexUser, RoleLibrary
from woosarr.db.session import alembic_config, make_engine, migrate
from woosarr.linking import PlexIdentity
from woosarr.services import inventory, link_service
from woosarr.services.sync import SyncService


class LiveFixTests(Base):
    def setUp(self):
        super().setUp()
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        self.gw = self.plex.gateway()
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)

    def test_owner_is_listed_and_can_link(self):
        with self.db.session() as s:
            owner = s.get(PlexUser, "1")
            self.assertTrue(owner.is_owner)
            r = link_service.link_by_identity(s, "d-owner", PlexIdentity("1", "owner"))
        self.assertTrue(r.ok, r.message)

    def test_owner_is_never_synced(self):
        with self.db.session() as s:
            s.add(DiscordMember(discord_user_id="d-owner", username="owner", role_ids=["r1"]))
            s.add(DiscordLink(plex_user_id="1", discord_user_id="d-owner", method="oauth", verified=True,
                              linked_by="self"))
            s.add(RoleLibrary(role_id="r1", library_id="2"))
            app_settings.update(s, {"dry_run": False})
        report = SyncService(self.db, self.plex.gateway).run_now(None, "test")
        self.assertEqual(report.errors, 0, report.error_messages)
        self.assertEqual(report.users_checked, 0)
        self.assertFalse([c for c in self.tv.calls if c[0] in ("put", "post", "delete")])

    def test_friends_without_a_share_are_listed(self):
        with self.db.session() as s:
            names = set(s.scalars(select(PlexUser.username).where(PlexUser.removed.is_(False))))
        self.assertEqual(names, {"owner", "alice", "bob", "kid"})  # nobody has a share in this fixture

    def test_write_check_on_user_with_no_libraries(self):
        result = self.plex.write_check("12")
        self.assertTrue(result["passed"], result)
        self.assertEqual([s["step"] for s in result["steps"]], ["add one library", "restore original"])
        self.assertNotIn("12", self.tv.shares)  # restored = share removed again (P0-03 c)
        writes = [c[0] for c in self.tv.calls if c[0] in ("put", "post", "delete")]
        self.assertEqual(writes, ["post", "delete"])  # share created, then the last library removed

    def test_write_check_refuses_the_owner(self):
        with self.assertRaises(ValueError):
            self.plex.write_check("1")


class OwnerApiTests(unittest.TestCase):
    def test_manual_grants_for_owner_rejected(self):
        h = AppHarness()
        try:
            c = h.setup_admin()
            with h.app.state.db.session() as s:
                s.add(PlexUser(plex_user_id="1", username="owner", is_owner=True))
                from woosarr.db.models import Library

                s.add(Library(library_id="5", title="Music", type="artist"))
            r = c.post("/api/v1/users/manual-grants",
                       json={"plex_user_ids": ["1"], "library_ids": ["5"], "action": "add"})
            self.assertEqual(r.status_code, 400)
            self.assertTrue(c.get("/api/v1/users/1").json()["is_owner"])
        finally:
            h.close()


class MigrationUpgradeTests(unittest.TestCase):
    def test_0001_database_upgrades_keeping_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            engine = make_engine(Path(tmp) / "app.db")
            command.upgrade(alembic_config(engine), "0001")
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO plex_users (plex_user_id, username, is_managed, removed, synced_at) "
                                  "VALUES ('7', 'existing', 0, 0, '2026-10-17 10:00:00')"))
            migrate(engine)
            cols = {c["name"] for c in inspect(engine).get_columns("plex_users")}
            self.assertIn("is_owner", cols)
            with engine.connect() as conn:
                row = conn.execute(text("SELECT username, is_owner FROM plex_users")).one()
            self.assertEqual(tuple(row), ("existing", 0))
            engine.dispose()


class CommunityFriendsTests(Base):
    """plex.tv/api/v2/friends is 410 Gone; friends may only come from the community
    GraphQL API, which has no numeric ids."""

    def setUp(self):
        super().setUp()
        self.tv.friends_mode = "community"
        self.tv.shares["11"] = {"id": 7, "sections": {101}}  # alice has a share; bob doesn't
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        self.gw = self.plex.gateway()

    def refresh(self):
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)

    def test_friend_without_share_listed_by_uuid_and_linkable(self):
        self.refresh()
        with self.db.session() as s:
            bob = s.scalar(select(PlexUser).where(PlexUser.username == "bob"))
            self.assertEqual(bob.plex_user_id, "uuid:uu12")
            r = link_service.link_by_username(s, "d-bob", "bob")
        self.assertTrue(r.ok, r.message)

    def test_verified_link_matches_by_username(self):
        self.refresh()
        with self.db.session() as s:
            r = link_service.link_by_identity(s, "d-bob", PlexIdentity("12", "bob"))
        self.assertTrue(r.ok, r.message)

    def test_sharing_invites_by_username_then_rekeys_keeping_the_link(self):
        self.refresh()
        with self.db.session() as s:
            app_settings.update(s, {"link_cooldown_seconds": 0})
            link_service.link_by_username(s, "d-bob", "bob")
        self.gw.set_user_libraries("uuid:uu12", ["2"])
        post = [c for c in self.tv.calls if c[0] == "post" and "shared_servers" in c[1]][-1]
        self.assertEqual(post[2]["shared_server"]["invited_email"], "bob")
        self.assertEqual(self.tv.shares["12"]["sections"], {102})
        self.refresh()  # plex.tv now reports bob with his numeric id
        with self.db.session() as s:
            self.assertIsNone(s.get(PlexUser, "uuid:uu12"))
            self.assertEqual(s.get(PlexUser, "12").username, "bob")
            link = s.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == "d-bob"))
            self.assertEqual(link.plex_user_id, "12")

    def test_uuid_user_reads_resolve_after_share(self):
        self.refresh()
        self.assertEqual(self.gw.get_user_library_ids("uuid:uu12"), frozenset())
        self.tv.shares["12"] = {"id": 8, "sections": {103}}
        self.assertEqual(self.gw.get_user_library_ids("uuid:uu12"), frozenset({"3"}))

    def test_no_friends_source_is_quiet_after_first_warning(self):
        self.tv.friends_mode = "none"
        import logging

        with self.assertLogs("woosarr.plex", level="WARNING") as cm:
            self.gw.list_users()
            self.gw.list_users()
            logging.getLogger("woosarr.plex").warning("sentinel")
        warnings = [m for m in cm.output if "no friends list available" in m]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(self.gw.state.ok)  # an optional source failing isn't a Plex outage


class RekeyMergeTests(Base):
    """Live: a friend (numeric id) had a share, lost it (row marked removed), was
    re-found as uuid:..., got a share again. The uuid row must merge into the numeric row."""

    def test_merge_into_existing_removed_row_keeps_uuid_link_and_grants(self):
        from woosarr.db.models import Library, ManualGrant

        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="12", username="bob", removed=True),
                       PlexUser(plex_user_id="uuid:uu12", username="bob"),
                       Library(library_id="2", title="TV", type="show"),
                       Library(library_id="3", title="Music", type="artist")])
            s.flush()
            s.add(DiscordLink(plex_user_id="uuid:uu12", discord_user_id="d-bob", method="username", verified=False,
                              linked_by="self"))
            s.add(ManualGrant(plex_user_id="uuid:uu12", library_id="3", granted_by="admin:x"))
            s.add(ManualGrant(plex_user_id="12", library_id="3", granted_by="admin:old"))  # duplicate
            s.add(ManualGrant(plex_user_id="uuid:uu12", library_id="2", granted_by="admin:x"))
        self.tv.shares["12"] = {"id": 8, "sections": {102}}  # bob is back in /api/users
        self.plex.complete_owner_login(OWNER)
        self.plex.select_server(MACHINE)
        with self.db.session() as s:
            inventory.refresh_all(s, self.plex.gateway())
        with self.db.session() as s:
            self.assertIsNone(s.get(PlexUser, "uuid:uu12"))
            bob = s.get(PlexUser, "12")
            self.assertFalse(bob.removed)
            link = s.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == "d-bob"))
            self.assertEqual(link.plex_user_id, "12")
            grants = {(g.library_id, g.granted_by) for g in s.scalars(select(ManualGrant))}
            self.assertEqual(grants, {("3", "admin:old"), ("2", "admin:x")})

    def test_both_linked_keeps_numeric_link(self):
        with self.db.session() as s:
            s.add_all([PlexUser(plex_user_id="12", username="bob", removed=True),
                       PlexUser(plex_user_id="uuid:uu12", username="bob")])
            s.flush()
            s.add(DiscordLink(plex_user_id="12", discord_user_id="d-old", method="oauth", verified=True,
                              linked_by="self"))
            s.add(DiscordLink(plex_user_id="uuid:uu12", discord_user_id="d-new", method="username", verified=False,
                              linked_by="self"))
            inventory.rekey_user(s, "uuid:uu12", "12")
        with self.db.session() as s:
            self.assertEqual([lk.discord_user_id for lk in s.scalars(select(DiscordLink))], ["d-old"])
