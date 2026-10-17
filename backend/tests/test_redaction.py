import io
import logging
import unittest

from woosarr.core import redaction

# Canary values: obviously fake, only used to prove they get scrubbed.
PLEX_CANARY = "CANARYplextoken1234567890"
DISCORD_CANARY = "MTIzNDU2Nzg5MDEyMzQ1" + "Njc4OQ" + "." + "GabcDE" + "." + "abcdefghijklmnopqrstuvwxyz0"
PASSWORD_CANARY = "hunter2-canary-pass"


def make_logger():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    redaction.install(handler)
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


class RedactionTests(unittest.TestCase):
    def setUp(self):
        redaction.registry.clear()

    def test_plex_token_in_url_and_header(self):
        log, out = make_logger()
        log.info("GET https://plex.tv/api?X-Plex-Token=%s", PLEX_CANARY)
        log.info("headers {'X-Plex-Token': '%s'}", PLEX_CANARY)
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_registered_exact_secret(self):
        redaction.register_secret("some-odd-secret-value")
        log, out = make_logger()
        log.info("value is some-odd-secret-value ok")
        self.assertNotIn("some-odd-secret-value", out.getvalue())

    def test_discord_token_shape(self):
        log, out = make_logger()
        log.error("login failed for %s", DISCORD_CANARY)
        self.assertNotIn(DISCORD_CANARY, out.getvalue())

    def test_authorization_header(self):
        log, out = make_logger()
        log.debug("Authorization: Bearer %s", PLEX_CANARY)
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_password_kv(self):
        log, out = make_logger()
        log.info("login payload password=%s user=admin", PASSWORD_CANARY)
        self.assertNotIn(PASSWORD_CANARY, out.getvalue())
        self.assertIn("user=admin", out.getvalue())

    def test_exception_traceback_is_scrubbed(self):
        redaction.register_secret(PLEX_CANARY)
        log, out = make_logger()
        try:
            raise RuntimeError(f"bad token {PLEX_CANARY}")
        except RuntimeError:
            log.exception("request failed")
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_normal_text_untouched(self):
        log, out = make_logger()
        log.info("Granted library Movies to user alice")
        self.assertIn("Granted library Movies to user alice", out.getvalue())


if __name__ == "__main__":
    unittest.main()
