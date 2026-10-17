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
