import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import inspect, select
from sqlalchemy.exc import IntegrityError, StatementError

from woosarr.db import models as m
from woosarr.db.session import Database, make_engine, migrate


class DbTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.engine = make_engine(Path(self.tmp.name) / "app.db")
        migrate(self.engine)
        self.db = Database(self.engine)

    def tearDown(self):
        self.engine.dispose()
        self.tmp.cleanup()

    def test_fresh_db_migrates_to_all_tables(self):
        tables = set(inspect(self.engine).get_table_names())
        self.assertLessEqual(set(m.Base.metadata.tables), tables)
        self.assertIn("alembic_version", tables)

    def test_migrations_match_models(self):
        # Fails when a model changes without a new migration.
        with self.engine.connect() as conn:
            ctx = MigrationContext.configure(conn, opts={"compare_type": True})
            self.assertEqual(compare_metadata(ctx, m.Base.metadata), [])

    def test_migrate_is_idempotent(self):
        migrate(self.engine)
        migrate(self.engine)

    def test_datetimes_round_trip_as_utc(self):
        with self.db.session() as s:
            s.add(m.AuditLog(actor="system", action="test"))
        with self.db.session() as s:
            row = s.scalars(select(m.AuditLog)).one()
            self.assertEqual(row.ts.tzinfo, UTC)
            self.assertLess(abs((datetime.now(UTC) - row.ts).total_seconds()), 5)

    def test_naive_datetime_rejected(self):
        with self.assertRaises(StatementError), self.db.session() as s:
            s.add(m.AuditLog(actor="system", action="x", ts=datetime.now()))

    def test_link_is_one_to_one(self):
        with self.db.session() as s:
            s.add_all([m.PlexUser(plex_user_id="p1", username="a"), m.PlexUser(plex_user_id="p2", username="b")])
            s.flush()
            s.add(m.DiscordLink(plex_user_id="p1", discord_user_id="d1", method="username", linked_by="self"))
        for plex, discord in (("p1", "d2"), ("p2", "d1")):
            with self.assertRaises(IntegrityError), self.db.session() as s:
                s.add(m.DiscordLink(plex_user_id=plex, discord_user_id=discord, method="username", linked_by="self"))

    def test_foreign_keys_cascade(self):
        with self.db.session() as s:
            s.add(m.PlexUser(plex_user_id="p1", username="a"))
            s.add(m.Library(library_id="1", title="Movies", type="movie"))
        with self.db.session() as s:
            s.add(m.ManualGrant(plex_user_id="p1", library_id="1", granted_by="admin:x"))
        with self.db.session() as s:
            s.delete(s.get(m.Library, "1"))
        with self.db.session() as s:
            self.assertEqual(s.scalars(select(m.ManualGrant)).all(), [])

    def test_no_token_columns_outside_secrets(self):
        # N9: links never persist Plex credentials; tokens only live encrypted in `secrets`.
        for table in m.Base.metadata.tables.values():
            if table.name == "secrets":
                continue
            for col in table.columns:
                if (table.name, col.name) == ("sessions", "csrf_token"):
                    continue  # per-session anti-CSRF value, not a credential
                self.assertNotIn("token", col.name.lower(), f"{table.name}.{col.name}")
