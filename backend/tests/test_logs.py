import json
import logging
import tempfile
import unittest
from pathlib import Path

from woosarr.core import logs, redaction
from woosarr.core.config import Settings


def canary() -> str:
    # Assembled at runtime so no secret-shaped literal sits in source.
    return "canary" + "Zq9" * 6


class LoggingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.settings = Settings(config_dir=Path(self.tmp.name), log_format="json")
        self.ring = logs.configure_logging(self.settings)
        self.log = logging.getLogger("woosarr.test")
        self.secret = canary()
        redaction.register_secret(self.secret)

    def tearDown(self):
        redaction.registry.clear()
        for h in logs._state.handlers:
            logging.getLogger().removeHandler(h)
            h.close()
        logs._state.handlers = []
        self.tmp.cleanup()

    def _flush(self):
        for h in logs._state.handlers:
            h.flush()

    def test_warnings_survive_a_restart(self):
        self.log.info("just info")
        self.log.warning("disk nearly full on %s", "/config")
        self.log.error("plex unreachable")
        self._flush()
        self.settings.log_file.write_text("")  # the main log rotated away; alerts.log still has them
        ring = logs.configure_logging(self.settings)  # what a container restart does
        problems, _ = ring.query(min_level=logging.WARNING)
        self.assertEqual([e.message for e in problems], ["disk nearly full on /config", "plex unreachable"])
        self.assertEqual([e.seq for e in problems], [1, 2])
        self.log.warning("after restart")
        self.assertEqual(ring.query(min_level=logging.WARNING)[0][-1].seq, 4)  # after the reload notice (INFO)

    def test_first_start_with_alerts_file_reloads_from_main_log(self):
        self.log.warning("from before the upgrade")
        self._flush()
        self.settings.alerts_file.unlink()  # an install from before alerts.log existed
        ring = logs.configure_logging(self.settings)
        self.assertEqual([e.message for e in ring.query(min_level=logging.WARNING)[0]], ["from before the upgrade"])

    def test_short_discord_gateway_lag_is_info_long_lag_stays_a_warning(self):
        gateway = logging.getLogger("discord.gateway")
        msg = "Can't keep up, shard ID %s websocket is %.1fs behind."
        gateway.warning(msg, None, 13.6)
        gateway.warning(msg, None, 75.0)
        entries = [e for e in self.ring.query(contains="keep up")[0]]
        self.assertEqual([(e.level, e.message[-13:]) for e in entries],
                         [("INFO", "13.6s behind."), ("WARNING", "75.0s behind.")])
        self.assertEqual([e.message for e in self.ring.query(min_level=logging.WARNING, contains="keep up")[0]],
                         ["Can't keep up, shard ID None websocket is 75.0s behind."])

    def test_canary_never_reaches_any_sink(self):
        self.log.info("token is %s", self.secret)
        try:
            raise ValueError(f"boom {self.secret}")
        except ValueError:
            self.log.exception("failed with %s", self.secret)
        self._flush()

        entries, _ = self.ring.query()
        dumped = json.dumps([e.to_dict() for e in entries])
        self.assertNotIn(self.secret, dumped)
        self.assertIn(redaction.REDACTED, dumped)
        self.assertTrue(any(e.exc and "ValueError" in e.exc for e in entries))

        text = self.settings.log_file.read_text()
        self.assertNotIn(self.secret, text)
        self.assertIn("ValueError", text)
        for line in text.splitlines():
            json.loads(line)  # file sink is JSON lines
        alerts = self.settings.alerts_file.read_text()
        self.assertNotIn(self.secret, alerts)
        self.assertEqual([json.loads(line)["level"] for line in alerts.splitlines()], ["ERROR"])  # warnings and up only

    def test_library_loggers_are_routed_and_redacted(self):
        logging.getLogger("uvicorn.access").info("GET /x?token=%s", self.secret)
        entries, _ = self.ring.query(contains="uvicorn")
        self.assertEqual(len(entries), 1)
        self.assertNotIn(self.secret, entries[0].message)

    def test_query_since_level_and_search(self):
        self.log.info("first")
        _, cursor = self.ring.query()
        self.log.warning("second needle")
        self.log.debug("third")
        newer, latest = self.ring.query(since=cursor)
        self.assertEqual([e.message for e in newer], ["second needle"])  # DEBUG below INFO root level
        self.assertEqual(latest, cursor + 1)
        self.assertEqual(len(self.ring.query(min_level=logging.WARNING)[0]), 1)
        self.assertEqual(len(self.ring.query(contains="NEEDLE")[0]), 1)

    def test_reconfigure_does_not_duplicate_handlers(self):
        before = len(logging.getLogger().handlers)
        logs.configure_logging(self.settings)
        self.assertEqual(len(logging.getLogger().handlers), before)
        self.log.info("once")
        self.assertEqual(len(logs.buffer().query(contains="once")[0]), 1)

    def test_buffer_is_bounded(self):
        ring = logs.RingBufferHandler(capacity=3)
        for i in range(10):
            ring.emit(logging.LogRecord("x", logging.INFO, __file__, 1, f"m{i}", None, None))
        entries, latest = ring.query()
        self.assertEqual([e.message for e in entries], ["m7", "m8", "m9"])
        self.assertEqual(latest, 10)


class LibraryLoggerCapTests(unittest.TestCase):
    def test_discord_internals_stay_at_info_when_app_is_debug(self):
        with tempfile.TemporaryDirectory() as tmp:
            ring = logs.configure_logging(Settings(config_dir=Path(tmp), log_level="DEBUG"))
            logging.getLogger("discord.gateway").debug("event dump")
            logging.getLogger("woosarr.sync").debug("our debug")
            messages = [e.message for e in ring.query()[0]]
            for h in logs._state.handlers:
                logging.getLogger().removeHandler(h)
                h.close()
            logs._state.handlers = []
        self.assertIn("our debug", messages)
        self.assertNotIn("event dump", messages)
