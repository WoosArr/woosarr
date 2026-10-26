"""Alerts that end by themselves leave the dashboard; the log lines stay, marked resolved by WooSarr."""

import logging
import unittest
from types import SimpleNamespace

import discord

from tests.helpers import AppHarness
from woosarr.services import self_resolve


def stale_interaction() -> discord.NotFound:
    return discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"code": 10062, "message": "Unknown interaction"})


class SelfResolveTests(unittest.TestCase):
    def setUp(self):
        self_resolve.reset()
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.before = self.open_alerts()  # the test app's own startup warnings

    def tearDown(self):
        self.h.close()
        self_resolve.reset()

    def open_alerts(self):
        found = {g["message"].splitlines()[0][:40] for g in self.c.get("/api/v1/errors").json() if g["count"] > 0}
        return found - getattr(self, "before", set())

    def test_over_when_logged_never_stays_on_the_dashboard(self):
        logging.getLogger("woosarr.stall").warning("WooSarr was paused for 6.0 s: nothing in the process ran.")
        logging.getLogger("woosarr.discord").warning("discord: /link from member 1 answered too late: x")
        logging.getLogger("urllib3.connectionpool").warning("Connection pool is full, discarding connection: x")
        try:
            raise stale_interaction()
        except discord.NotFound:
            logging.getLogger("discord.app_commands.tree").exception(
                "Ignoring exception in autocomplete for %r (Guild: %s, User: %s)", "request", 1, 2)
        try:
            raise RuntimeError("a real bug")
        except RuntimeError:
            logging.getLogger("discord.app_commands.tree").exception("Ignoring exception in command %r", "link")
        logging.getLogger("woosarr.app").error("unhandled error request_id=abc GET /api/v1/x")
        with self.assertLogs("woosarr.alerts", "INFO") as logs:
            still_open = self.open_alerts()
        self.assertEqual(still_open, {"Ignoring exception in command 'link'", "unhandled error request_id=abc GET /api/"})
        # 3, not 4: a suggestion Discord had dropped never becomes an alert at all (discord_bot's log filter).
        self.assertEqual(len(logs.output), 3)
        self.assertIn("alert resolved by WooSarr (the member was asked to try again)", "\n".join(logs.output))
        groups = self.c.get("/api/v1/errors").json()
        paused = next(g for g in groups if g["message"].startswith("WooSarr was paused"))
        self.assertEqual((paused["count"], paused["resolved_by"]), (0, "WooSarr: it was over when it was logged"))
        # The log lines stay, marked.
        entries = self.c.get("/api/v1/logs", params={"level": "WARNING", "limit": 50}).json()["entries"]
        line = lambda start: next(e for e in entries if e["message"].startswith(start))
        self.assertEqual(line("WooSarr was paused")["resolved_by"], "WooSarr: it was over when it was logged")
        self.assertIsNone(line("unhandled error")["resolved_by"])
        self.assertEqual(self.c.get("/api/v1/status").json()["open_errors"], 2)

    def test_no_admin_yet_clears_once_one_exists(self):
        h = AppHarness()
        try:
            logging.getLogger("woosarr.app").warning("no admin account yet; open the web UI to finish first-run setup")
            c = h.setup_admin()
            msgs = [g["message"] for g in c.get("/api/v1/errors").json() if g["count"] > 0]
            self.assertFalse(any(m.startswith("no admin account yet") for m in msgs))
        finally:
            h.close()

    def test_connection_alerts_wait_for_the_service_to_answer_again(self):
        seerr = logging.getLogger("woosarr.seerr")
        seerr.warning("seerr GET /request/190 failed: SSLError")
        self.assertIn("seerr GET /request/190 failed: SSLError", self.open_alerts())
        self_resolve.recovered("seerr")  # RequestService._ok(): Seerr answered
        self.assertEqual(self.open_alerts(), set())
        seerr.warning("seerr GET /request/190 failed: SSLError")  # fails again: shows again
        self.assertIn("seerr GET /request/190 failed: SSLError", self.open_alerts())

    def test_reaction_post_alert_clears_only_for_that_post(self):
        logging.getLogger("woosarr.discord").warning("reaction post 3 not published: bot lacks Add Reactions in #x")
        self_resolve.recovered("reaction_post:4")
        self.assertEqual(len(self.open_alerts()), 1)
        self_resolve.recovered("reaction_post:3")
        self.assertEqual(self.open_alerts(), set())

    def test_two_requests_at_once_resolve_the_same_alert(self):
        # Live in e2e: the dashboard asks for /errors and /status together; both resolved the same
        # alert and the second insert failed on the primary key (500).
        from woosarr.services import error_digest
        logging.getLogger("urllib3.connectionpool").warning("Connection pool is full, discarding connection: y")
        db = self.h.app.state.db
        with db.session() as s1, db.session() as s2:
            g1, g2 = error_digest._groups(s1), error_digest._groups(s2)
            self.assertTrue(self_resolve.apply(s1, g1))
            s1.commit()
            self.assertTrue(self_resolve.apply(s2, g2))  # the same group, already resolved meanwhile
        self.assertEqual(self.open_alerts(), set())

    def test_services_report_recovery(self):
        from woosarr.services.media_requests import RequestService
        svc = RequestService(self.h.app.state.db, self.h.app.state.secrets)
        svc._ok()
        self.assertIsNotNone(self_resolve.recovered_at("seerr"))
