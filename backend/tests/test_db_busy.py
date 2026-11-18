"""A locked database (live 2026-11-18): pages keep working, writes get 503 not 500, one warning that clears itself."""

import logging
import sqlite3
import time
import unittest
from datetime import timedelta
from unittest import mock

from sqlalchemy import exc as sa_exc
from sqlalchemy import update

from tests.helpers import AppHarness
from woosarr.core import logs
from woosarr.db import busy
from woosarr.db import session as db_session
from woosarr.db.models import Session as DbSession
from woosarr.db.models import utcnow
from woosarr.services import self_resolve

SHORT_MS = 300  # the usual 30 s lock wait, shortened so the tests are quick


class LockedDatabaseTests(unittest.TestCase):
    def setUp(self):
        busy.reset()
        self_resolve.reset()
        patch = mock.patch.object(db_session, "BUSY_TIMEOUT_MS", SHORT_MS)
        patch.start()
        self.addCleanup(patch.stop)
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.path = self.h.app.state.db.engine.url.database
        self.holder: sqlite3.Connection | None = None
        self.c.get("/api/v1/errors")  # resolve the test app's startup alerts now, not mid-test

    def tearDown(self):
        self.unlock()
        self.h.close()
        busy.reset()
        self_resolve.reset()

    def lock(self):
        """Another write holds SQLite's lock, as the stalled one did."""
        self.holder = sqlite3.connect(self.path, isolation_level=None)
        self.holder.execute("BEGIN IMMEDIATE")

    def unlock(self):
        if self.holder is not None:
            self.holder.execute("ROLLBACK")
            self.holder.close()
            self.holder = None

    def age_sessions(self, minutes=5):
        with self.h.app.state.db.session() as s:
            s.execute(update(DbSession).values(last_seen_at=utcnow() - timedelta(minutes=minutes)))

    def last_seen(self):
        with self.h.app.state.db.session() as s:
            return s.query(DbSession).one().last_seen_at

    def test_pages_load_while_another_write_holds_the_lock(self):
        self.age_sessions()
        before = self.last_seen()
        self.lock()
        with self.assertLogs("woosarr.db", logging.INFO) as logged:
            started = time.monotonic()
            r = self.c.get("/api/v1/status")
            took = time.monotonic() - started
        self.assertEqual(r.status_code, 200, r.text)
        self.assertLess(took, 5)
        self.assertIn("session last-seen time not saved", "\n".join(logged.output))
        self.unlock()
        self.assertEqual(self.last_seen(), before)  # skipped, not half-written
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)
        self.assertGreater(self.last_seen(), before)  # the next request saves it

    def test_last_seen_is_saved_normally(self):
        self.age_sessions()
        before = self.last_seen()
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)
        self.assertGreater(self.last_seen(), before)

    def test_a_write_gets_503_and_one_warning_that_clears_itself(self):
        self.lock()
        first = self.c.patch("/api/v1/settings", json={"bot_status": "one"})
        second = self.c.patch("/api/v1/settings", json={"bot_status": "two"})
        for r in (first, second):
            self.assertEqual(r.status_code, 503, r.text)
            self.assertEqual(r.json()["error"], "database_busy")
            self.assertEqual(r.headers["Retry-After"], "5")
        logs.flush()
        entries = self.c.get("/api/v1/logs", params={"q": "database busy"}).json()["entries"]
        self.assertEqual(sorted(e["level"] for e in entries), ["INFO", "WARNING"])
        errors = self.c.get("/api/v1/logs", params={"level": "ERROR"}).json()["entries"]
        self.assertEqual(errors, [])  # neither WooSarr nor uvicorn calls it a crash
        self.unlock()

        def open_busy():
            return [g for g in self.c.get("/api/v1/errors").json()
                    if g["message"].startswith("database busy") and g["count"] > 0]

        self.assertEqual(len(open_busy()), 1)
        time.sleep(0.01)  # recovery must come after the warning
        self.assertEqual(self.c.patch("/api/v1/settings", json={"bot_status": "three"}).status_code, 200)
        self.assertEqual(open_busy(), [])

    def test_a_read_only_request_does_not_count_as_recovery(self):
        busy.refused("PATCH", "/x", "locked", "rid")
        self.assertEqual(self.c.get("/api/v1/status").status_code, 200)  # session is fresh: no write
        self.assertIsNone(self_resolve.recovered_at(busy.RECOVERY_KEY))


class ReasonTests(unittest.TestCase):
    def test_recognises_lock_and_full_pool_only(self):
        locked = sa_exc.OperationalError("UPDATE x", {}, sqlite3.OperationalError("database is locked"))
        other = sa_exc.OperationalError("SELECT", {}, sqlite3.OperationalError("disk I/O error"))
        self.assertIn("locked", busy.reason(locked))
        self.assertIn("connection", busy.reason(sa_exc.TimeoutError("QueuePool limit")))
        self.assertIsNone(busy.reason(other))
        self.assertIsNone(busy.reason(ValueError("database is locked")))


class RuleTests(unittest.TestCase):
    def test_still_disconnected_clears_when_the_bot_reconnects(self):
        found = self_resolve.rule_for(
            "woosarr.discord", "discord gateway still disconnected after 60 s; discord.py keeps retrying", None)
        self.assertIsNotNone(found)
        self.assertEqual(found[1], "discord")


if __name__ == "__main__":
    unittest.main()
