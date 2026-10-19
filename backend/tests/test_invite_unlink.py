import tempfile
import unittest
from pathlib import Path

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
    Library,
    ManualGrant,
    PlexInvite,
    RoleLibrary,
)
from woosarr.db.session import Database, make_engine, migrate
from woosarr.plex_gateway import FakePlexGateway, PlexLibrary
from woosarr.plex_gateway import PlexUser as GwUser
from woosarr.services import inventory, invites
from woosarr.services.discord_bot import DiscordService
from woosarr.services.sync import SyncService


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)
        self.gw = FakePlexGateway(
            users=[GwUser("11", "alice", "alice@x.com")],
            libraries=[PlexLibrary("1", "Movies", "movie"), PlexLibrary("2", "TV", "show"),
                       PlexLibrary("3", "Kids", "movie")],
            shares={"11": {"1", "2", "3"}},
        )
        with self.db.session() as s:
            inventory.refresh_all(s, self.gw)
            s.get(Library, "3").is_exempt = True
            s.add_all([DiscordMember(discord_user_id="d1", username="alice", role_ids=["movies", "tv"]),
                       DiscordMember(discord_user_id="d2", username="newbie", role_ids=["movies", "inviter"]),
                       DiscordMember(discord_user_id="d3", username="nobody", role_ids=[])])
            s.add_all([RoleLibrary(role_id="movies", library_id="1"), RoleLibrary(role_id="tv", library_id="2"),
                       RoleLibrary(role_id="tv", library_id="3")])
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d1", method="oauth", verified=True,
                              linked_by="self"))
            app_settings.update(s, {"dry_run": False, "link_cooldown_seconds": 0, "invite_role_ids": ["inviter"]})
        self.sync = SyncService(self.db, lambda: self.gw)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def invite(self, actor="d2", target=None, ident="newbie@x.com", admin=False):
        with self.db.session() as s:
            return invites.create(s, self.gw, actor_discord_id=actor, actor_is_admin=admin,
                                  target_discord_id=target or actor, identifier=ident)


class InviteTests(Base):
    def test_member_with_allowed_role_invites_self_with_role_libraries(self):
        r = self.invite()
        self.assertTrue(r.ok, r.message)
        self.assertEqual(self.gw.invites, [("newbie@x.com", frozenset({"1"}))])
        with self.db.session() as s:
            inv = s.scalars(select(PlexInvite)).one()
            self.assertEqual((inv.status, inv.verified, inv.library_ids), ("pending", False, ["1"]))

    def test_exempt_libraries_never_in_an_invite(self):
        with self.db.session() as s:
            s.get(DiscordMember, "d2").role_ids = ["movies", "tv", "inviter"]
        self.invite()
        self.assertEqual(self.gw.invites[0][1], frozenset({"1", "2"}))  # "3" is exempt

    def test_role_required_for_self_invite(self):
        r = self.invite(actor="d3", ident="x@x.com")
        self.assertFalse(r.ok)
        self.assertIn("roles don't allow", r.message)
        self.assertEqual(self.gw.invites, [])

    def test_members_cannot_invite_others_but_admins_can(self):
        self.assertIn("Only server admins", self.invite(actor="d2", target="d3", ident="x@x.com").message)
        with self.db.session() as s:
            s.get(DiscordMember, "d3").role_ids = ["tv"]
        r = self.invite(actor="d2", target="d3", ident="nobody@x.com", admin=True)
        self.assertTrue(r.ok, r.message)
        with self.db.session() as s:
            self.assertTrue(s.scalars(select(PlexInvite)).one().verified)

    def test_refusals(self):
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": True})
        self.assertIn("dry-run", self.invite().message)
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": False})
        self.assertIn("already on this Plex server", self.invite(ident="ALICE@x.com").message)
        self.assertIn("already linked", self.invite(actor="d1", ident="z@x.com", admin=True).message)
        self.assertIn("don't give access", self.invite(actor="d1", target="d3", ident="z@x.com", admin=True).message)
        self.assertIn("email or username", self.invite(ident="not valid!").message)
        self.assertTrue(self.invite().ok)
        self.assertIn("already a pending invite", self.invite(ident="other@x.com").message)
        with self.db.session() as s:
            fails = s.scalars(select(AuditLog.details).where(AuditLog.action == "invite_failed")).all()
        self.assertGreaterEqual(len(fails), 5)

    def test_plex_error_is_reported(self):
        def boom(*_a):
            raise RuntimeError("(400) bad_request")

        self.gw.invite = boom
        r = self.invite()
        self.assertFalse(r.ok)
        self.assertIn("didn't accept", r.message)

    def test_accepted_invite_links_and_syncs(self):
        self.invite()
        # The invitee accepts: plex.tv now lists them as a friend with the invited share.
        self.gw.users.append(GwUser("22", "newbie", "newbie@x.com"))
        self.gw.shares["22"] = {"1"}
        report = self.sync.run_now(None, "test")
        self.assertEqual(report.errors, 0, report.error_messages)
        with self.db.session() as s:
            link = s.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == "d2"))
            self.assertEqual((link.plex_user_id, link.method, link.verified), ("22", "invite", False))
            self.assertEqual(s.scalars(select(PlexInvite.status)).one(), "accepted")
        self.assertEqual(self.gw.shares["22"], {"1"})  # roles give Movies; nothing extra


class UnlinkTests(Base):
    def service(self):
        return DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())), self.sync.trigger, None,
                              self.sync)

    def linked(self, discord_id):
        with self.db.session() as s:
            return s.scalar(select(DiscordLink).where(DiscordLink.discord_user_id == discord_id)) is not None

    def test_drop_role_access_policy(self):
        with self.db.session() as s:
            s.add(ManualGrant(plex_user_id="11", library_id="2", granted_by="admin:x"))
        msg = self.service().unlink_member("d1")
        self.assertIn("Role-granted libraries were removed", msg)
        self.assertEqual(self.gw.shares["11"], {"2", "3"})  # Movies (role) gone; manual TV + exempt Kids kept
        self.assertFalse(self.linked("d1"))

    def test_keep_all_policy(self):
        with self.db.session() as s:
            app_settings.update(s, {"unlink_policy": "keep_all"})
        msg = self.service().unlink_member("d1")
        self.assertIn("stays as it is", msg)
        self.assertEqual(self.gw.shares["11"], {"1", "2", "3"})
        self.assertFalse(self.linked("d1"))

    def test_dry_run_unlinks_without_touching_plex(self):
        with self.db.session() as s:
            app_settings.update(s, {"dry_run": True})
        msg = self.service().unlink_member("d1")
        self.assertIn("dry-run", msg)
        self.assertEqual(self.gw.shares["11"], {"1", "2", "3"})
        self.assertFalse(self.linked("d1"))

    def test_not_linked(self):
        self.assertIn("isn't linked", self.service().unlink_member("d3"))

    def test_audited(self):
        self.service().unlink_member("d1")
        with self.db.session() as s:
            entry = s.scalar(select(AuditLog).where(AuditLog.action == "unlink_self"))
        self.assertEqual((entry.actor, entry.target_id, entry.details["policy"]),
                         ("discord:d1", "11", "drop_role_access"))


class InviteApiTests(unittest.TestCase):
    def test_list_and_cancel(self):
        h = AppHarness()
        try:
            c = h.setup_admin()
            with h.app.state.db.session() as s:
                s.add(PlexInvite(identifier="n@x.com", discord_user_id="d2", invited_by="discord:d2",
                                 library_ids=["1"]))
            rows = c.get("/api/v1/invites", params={"status": "pending"}).json()
            self.assertEqual(len(rows), 1)
            self.assertEqual(c.delete(f"/api/v1/invites/{rows[0]['id']}").json()["status"], "cancelled")
            self.assertEqual(c.get("/api/v1/invites", params={"status": "pending"}).json(), [])
            r = c.patch("/api/v1/settings", json={"unlink_policy": "keep_all", "invite_role_ids": ["123"]})
            self.assertEqual((r.json()["unlink_policy"], r.json()["invite_role_ids"]), ("keep_all", ["123"]))
        finally:
            h.close()


class FamilyAccountTests(Base):
    """A Plex account shared by several Discord members (a family)."""

    def setUp(self):
        super().setUp()
        with self.db.session() as s:
            s.get(DiscordMember, "d1").role_ids = ["tv"]
            s.get(DiscordMember, "d2").role_ids = ["movies"]
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d2", method="admin", verified=True,
                              linked_by="admin:x"))
        self.gw.shares["11"] = {"3"}

    def service(self):
        return DiscordService(self.db, SecretStore(SecretBox(Fernet.generate_key())), lambda *a, **k: None, None,
                              self.sync)

    def members(self):
        with self.db.session() as s:
            return sorted(x.discord_user_id for x in s.scalars(select(DiscordLink).where(DiscordLink.plex_user_id == "11")))

    def test_access_is_union_of_members_roles(self):
        self.sync.run_now(None, "test")
        self.assertEqual(self.gw.shares["11"], {"1", "2", "3"})  # Movies (d2) + TV (d1) + exempt Kids

    def test_leave_drop_role_access_removes_only_that_members_libraries(self):
        self.sync.run_now(None, "first")
        with self.db.session() as s:
            s.get(DiscordMember, "d1").in_guild = False
        self.sync.run_now(["11"], "d1 left")
        self.assertEqual(self.gw.shares["11"], {"1", "3"})  # TV went with d1; d2's Movies stay

    def test_leave_keep_all_keeps_their_last_roles(self):
        self.sync.run_now(None, "first")
        with self.db.session() as s:
            app_settings.update(s, {"leave_policy": "keep_all"})
            s.get(DiscordMember, "d1").in_guild = False
            s.get(DiscordMember, "d2").role_ids = []  # d2 (still here) loses Movies
        self.sync.run_now(["11"], "changes")
        self.assertEqual(self.gw.shares["11"], {"2", "3"})  # d1's TV kept; d2's Movies follow d2's roles

    def test_unlink_drop_role_access_with_others_remaining(self):
        self.sync.run_now(None, "first")
        msg = self.service().unlink_member("d1")
        self.assertIn("stays linked to the 1 other member", msg)
        self.sync.run_now(["11"], "after unlink")
        self.assertEqual(self.gw.shares["11"], {"1", "3"})
        self.assertEqual(self.members(), ["d2"])

    def test_unlink_keep_all_with_others_remaining_keeps_libraries_as_manual_grants(self):
        self.sync.run_now(None, "first")
        with self.db.session() as s:
            app_settings.update(s, {"unlink_policy": "keep_all"})
        msg = self.service().unlink_member("d1")
        self.assertIn("1 library only your roles gave stay as manual grants", msg)
        self.sync.run_now(["11"], "after unlink")
        self.assertEqual(self.gw.shares["11"], {"1", "2", "3"})
        with self.db.session() as s:
            grants = [g.library_id for g in s.scalars(select(ManualGrant).where(ManualGrant.plex_user_id == "11"))]
            kept = s.scalar(select(AuditLog).where(AuditLog.action == "manual_grant_kept"))
        self.assertEqual(grants, ["2"])
        self.assertEqual(kept.details["library_ids"], ["2"])

    def test_last_member_unlinking_works_as_before(self):
        self.service().unlink_member("d2")
        msg = self.service().unlink_member("d1")
        self.assertIn("Role-granted libraries were removed", msg)
        self.assertEqual(self.members(), [])


class FamilyJoinTests(Base):
    def join(self, discord_id, method="oauth"):
        from woosarr.linking import PlexIdentity
        from woosarr.services import link_service
        with self.db.session() as s:
            if method == "oauth":
                return link_service.link_by_identity(s, discord_id, PlexIdentity("11", "alice"))
            return link_service.link_by_username(s, discord_id, "alice")

    def members(self):
        with self.db.session() as s:
            return sorted((x.discord_user_id, x.verified)
                          for x in s.scalars(select(DiscordLink).where(DiscordLink.plex_user_id == "11")))

    def test_admin_only_mode_refuses_sign_in_joins(self):
        r = self.join("d2")
        self.assertFalse(r.ok)
        self.assertIn("ask an admin to add you", r.message)
        self.assertEqual(self.members(), [("d1", True)])

    def test_verified_mode_lets_sign_in_join_but_not_typed_usernames(self):
        with self.db.session() as s:
            app_settings.update(s, {"family_link_mode": "verified"})
        self.assertFalse(self.join("d3", method="username").ok)
        r = self.join("d2")
        self.assertTrue(r.ok, r.message)
        self.assertIn("share with 1 other member", r.message)
        self.assertEqual(self.members(), [("d1", True), ("d2", True)])

    def test_sign_in_still_takes_over_unverified_claims(self):
        with self.db.session() as s:
            s.query(DiscordLink).delete()
            s.add(DiscordLink(plex_user_id="11", discord_user_id="d1", method="username", verified=False,
                              linked_by="self"))
        r = self.join("d2")  # admin-only mode: no verified member blocks it
        self.assertTrue(r.ok, r.message)
        self.assertEqual(self.members(), [("d2", True)])
