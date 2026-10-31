"""Libraries removed from Plex and roles deleted in Discord: what pointed at them is forgotten (P3-08)."""

import tempfile
import unittest
from pathlib import Path

from sqlalchemy import select

from tests.helpers import AppHarness
from woosarr.core import app_settings, kv
from woosarr.db.models import (
    AuditLog,
    DiscordLink,
    DiscordMember,
    DiscordRole,
    Library,
    LibraryState,
    ManualGrant,
    ReactionPost,
    RoleLibrary,
)
from woosarr.db.session import Database, make_engine, migrate
from woosarr.plex_gateway import FakePlexGateway, PlexLibrary
from woosarr.plex_gateway import PlexUser as GwUser
from woosarr.services import discord_cache, inventory
from woosarr.services.discord_cache import RoleData
from woosarr.services.sync import SyncService

MOVIES, TV, FOURK = "100000000000000001", "100000000000000002", "100000000000000004"
LAYOUT = "dashboard.layout.1"


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.gw = FakePlexGateway(
            users=[GwUser("11", "alice", "alice@x.com"), GwUser("12", "bob", "bob@x.com")],
            libraries=[PlexLibrary("1", "Movies", "movie"), PlexLibrary("2", "TV", "show"),
                       PlexLibrary("36", "4K Movies", "movie")],
            shares={"11": {"1", "2", "36"}, "12": {"36"}},
        )
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)
            s.add_all([DiscordRole(role_id=MOVIES, name="Movies"), DiscordRole(role_id=TV, name="TV"),
                       DiscordRole(role_id=FOURK, name="4K")])
            s.add(DiscordMember(discord_user_id="d1", username="alice", role_ids=[MOVIES, TV, FOURK]))
            s.add_all([RoleLibrary(role_id=MOVIES, library_id="1"), RoleLibrary(role_id=TV, library_id="2"),
                       RoleLibrary(role_id=FOURK, library_id="36")])
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d1", method="oauth", verified=True, linked_by="self"))
            s.add(ManualGrant(plex_user_id="12", library_id="36", granted_by="admin:x"))
            s.add(ReactionPost(id=1, title="Pick", content="x",
                               options=[{"emoji": "📺", "role_id": TV}, {"emoji": "🎬", "role_id": MOVIES}]))
            kv.put(s, LAYOUT, [{"id": "library_access", "filter": {"libraries": ["36"]}},
                               {"id": "library_access-2", "filter": {"libraries": ["1", "36"]}},
                               {"id": "role_members", "filter": {"roles": [TV, MOVIES]}}])
            app_settings.update(s, {"dry_run": False, "request_role_ids": [TV], "verified_role_id": TV})
        self.sync = SyncService(self.db, lambda: self.gw)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def audit(self, action):
        with self.db.session() as s:
            return list(s.scalars(select(AuditLog).where(AuditLog.action == action)))


class RemovedLibraryTests(Base):
    def remove_4k(self):
        self.gw.libraries = [lib for lib in self.gw.libraries if lib.library_id != "36"]
        for shares in self.gw.shares.values():
            shares.discard("36")  # Plex drops a deleted library from everyone's shares

    def test_its_mappings_grants_and_filters_are_forgotten(self):
        self.sync.run_now(None, "first")
        self.remove_4k()
        self.gw.writes.clear()
        self.sync.run_now(None, "periodic")
        self.assertFalse(any("36" in libs for _uid, libs in self.gw.writes))  # never written to Plex
        with self.db.session() as s:
            self.assertTrue(s.get(Library, "36").removed)  # kept, so the audit trail keeps its name
            self.assertEqual(s.scalars(select(RoleLibrary).where(RoleLibrary.library_id == "36")).all(), [])
            self.assertEqual(s.scalars(select(ManualGrant)).all(), [])
            self.assertEqual(s.scalars(select(LibraryState).where(LibraryState.library_id == "36")).all(), [])
            self.assertIsNotNone(s.get(DiscordRole, FOURK))  # the role itself still exists in Discord
            self.assertEqual([w.get("filter") for w in kv.get(s, LAYOUT)],
                             [None, {"libraries": ["1"]}, {"roles": [TV, MOVIES]}])
        (entry,) = self.audit("library_forgotten")
        self.assertEqual((entry.target_id, entry.details["role_ids"], entry.details["plex_user_ids"]),
                         ("36", [FOURK], ["12"]))
        self.sync.run_now(None, "again")
        self.assertEqual(len(self.audit("library_forgotten")), 1)  # once

    def test_an_empty_library_list_from_plex_removes_nothing(self):
        self.gw.libraries = []
        with self.assertLogs("woosarr.inventory", "WARNING"):
            self.sync.run_now(None, "periodic")
        with self.db.session() as s:
            self.assertFalse(any(lib.removed for lib in s.scalars(select(Library))))
            self.assertEqual(len(s.scalars(select(RoleLibrary)).all()), 3)


class DeletedRoleTests(Base):
    def delete_tv(self):
        with self.db.session() as s:
            self.assertEqual(discord_cache.role_deleted(s, TV), ["11"])

    def test_access_goes_first_then_the_references(self):
        self.sync.run_now(None, "first")
        self.delete_tv()
        self.sync.run_now(None, "periodic")
        self.assertEqual(self.gw.shares["11"], {"1", "36"})  # TV only came from the deleted role
        with self.db.session() as s:
            self.assertIsNone(s.get(DiscordRole, TV))
            self.assertEqual(s.scalars(select(RoleLibrary).where(RoleLibrary.role_id == TV)).all(), [])
            st = app_settings.load(s)
            self.assertEqual((st.request_role_ids, st.verified_role_id), ([], None))
            self.assertEqual([o["role_id"] for o in s.get(ReactionPost, 1).options], [MOVIES])
            self.assertEqual(kv.get(s, LAYOUT)[2]["filter"], {"roles": [MOVIES]})
        (entry,) = self.audit("role_removed")
        self.assertEqual((entry.actor, entry.target_id, entry.details["library_ids"]), ("system", TV, ["2"]))
        self.sync.run_now(None, "again")
        self.assertEqual(self.gw.shares["11"], {"1", "36"})

    def test_kept_in_dry_run(self):
        self.delete_tv()
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": True})
        self.sync.run_now(None, "periodic")
        with self.db.session() as s:
            self.assertTrue(s.get(DiscordRole, TV).deleted)
            self.assertEqual(len(s.scalars(select(RoleLibrary).where(RoleLibrary.role_id == TV)).all()), 1)
        self.assertEqual(self.audit("role_removed"), [])

    def test_kept_when_the_sync_had_errors(self):
        self.delete_tv()
        self.gw.get_all_user_library_ids = None  # batch read unavailable: per-user reads below
        self.gw.get_user_library_ids = lambda uid: (_ for _ in ()).throw(RuntimeError("plex down"))
        self.sync.run_now(None, "periodic")
        with self.db.session() as s:
            self.assertTrue(s.get(DiscordRole, TV).deleted)

    def test_mapped_ids_never_seen_in_discord_are_left_alone(self):
        with self.db.session() as s:
            s.add(RoleLibrary(role_id="200000000000000009", library_id="1"))
        self.sync.run_now(None, "periodic")
        with self.db.session() as s:
            self.assertIsNotNone(s.get(RoleLibrary, ("200000000000000009", "1")))

    def test_full_discord_sync_marks_missing_roles_deleted_but_not_from_an_empty_list(self):
        with self.db.session() as s:
            discord_cache.full_sync(s, [], [])
            self.assertFalse(any(r.deleted for r in s.scalars(select(DiscordRole))))
            discord_cache.full_sync(s, [RoleData(MOVIES, "Movies", 0, 1, False), RoleData(FOURK, "4K", 0, 2, False)],
                                    [])
            self.assertTrue(s.get(DiscordRole, TV).deleted)
            self.assertFalse(s.get(DiscordRole, MOVIES).deleted)


class RemovedLibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        with self.h.app.state.db.session() as s:
            s.add_all([Library(library_id="1", title="Movies", type="movie"),
                       Library(library_id="36", title="4K Movies", type="movie", removed=True),
                       DiscordRole(role_id=MOVIES, name="Movies")])

    def tearDown(self):
        self.h.close()

    def test_cannot_map_or_grant_a_removed_library(self):
        r = self.c.put(f"/api/v1/roles/{MOVIES}/libraries", json={"library_ids": ["1", "36"]})
        self.assertEqual(r.status_code, 404)
        self.assertIn("36", r.json()["detail"])
        self.assertEqual(self.c.put(f"/api/v1/roles/{MOVIES}/libraries", json={"library_ids": ["1"]}).status_code, 200)
