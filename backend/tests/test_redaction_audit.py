"""Redaction audit (plan P10-02): canary secrets in the places they really leak.

HTTP client debug logs (urllib3/requests used by plexapi, aiohttp used by
discord.py), exception tracebacks, and the API's 500 handler must never write a
registered secret or a token-shaped value to any sink.
"""

import logging
import unittest

from tests.helpers import AppHarness
from woosarr.core import logs, redaction


def canary(tag: str) -> str:
    return f"{tag}" + "Qx7" * 8  # assembled at runtime; not a real token


class RedactionAuditTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        logging.getLogger().setLevel(logging.DEBUG)
        self.plex = canary("plex")
        self.bot = canary("bot")
        redaction.register_secret(self.plex)
        redaction.register_secret(self.bot)

    def tearDown(self):
        redaction.registry.clear()
        self.h.close()

    def sinks(self) -> str:
        for handler in logs._state.handlers:
            handler.flush()
        ring = "\n".join(f"{e.message}\n{e.exc or ''}" for e in logs.buffer().query(limit=5000)[0])
        return ring + self.h.settings.log_file.read_text()

    def test_http_client_debug_logs(self):
        lines = {
            "urllib3.connectionpool": ('https://plex.tv:443 "GET /api/v2/user?X-Plex-Token=%s HTTP/1.1" 200', self.plex),
            "requests": ("headers {'X-Plex-Token': '%s'}", self.plex),
            "plexapi": ("GET http://10.0.0.5:32400/library/sections?X-Plex-Token=%s", self.plex),
            "aiohttp.client": ("Authorization: Bot %s", self.bot),
            "discord.http": ("POST https://discord.com/api/v10/x with {'Authorization': 'Bot %s'}", self.bot),
            "httpx": ("Request headers authorization: Bearer %s", self.plex),
        }
        for name, (fmt, secret) in lines.items():
            logging.getLogger(name).debug(fmt, secret)
        out = self.sinks()
        self.assertNotIn(self.plex, out)
        self.assertNotIn(self.bot, out)
        self.assertGreaterEqual(out.count(redaction.REDACTED), len(lines))

    def test_unregistered_token_shapes_in_urls(self):
        unknown = canary("unk")
        logging.getLogger("urllib3.connectionpool").debug("GET /x?token=%s&a=1", unknown)
        logging.getLogger("plexapi").debug("x-plex-token: %s", unknown)
        self.assertNotIn(unknown, self.sinks())

    def test_exception_text_and_traceback(self):
        try:
            raise ConnectionError(f"failed calling https://plex.tv/?X-Plex-Token={self.plex}")
        except ConnectionError:
            logging.getLogger("woosarr.sync").exception("plex call failed for %s", self.bot)
        out = self.sinks()
        self.assertIn("ConnectionError", out)
        self.assertNotIn(self.plex, out)
        self.assertNotIn(self.bot, out)

    def test_api_500_handler(self):
        app = self.h.app

        @app.get("/api/v1/_leak", include_in_schema=False)
        def leak():
            raise RuntimeError(f"db password={self.plex}")

        app.router.routes.insert(0, app.router.routes.pop())
        r = self.c.get("/api/v1/_leak")
        self.assertEqual(r.status_code, 500)
        self.assertNotIn(self.plex, r.text)
        out = self.sinks()
        self.assertIn("RuntimeError", out)
        self.assertNotIn(self.plex, out)

    def test_stored_secrets_registered_at_startup(self):
        with self.h.app.state.db.session() as s:
            self.h.app.state.secrets.set(s, "plex.token", canary("stored"))
        redaction.registry.clear()
        # Restart-equivalent: a new app on the same /config re-registers stored values.
        from woosarr.main import create_app

        app2 = create_app(self.h.settings)
        try:
            logging.getLogger("x").info("value %s", canary("stored"))
            self.assertNotIn(canary("stored"), self.sinks())
        finally:
            app2.state.db.engine.dispose()

    def test_secrets_never_in_api_responses(self):
        with self.h.app.state.db.session() as s:
            self.h.app.state.secrets.set(s, "plex.token", self.plex)
        for path in ("/api/v1/status", "/api/v1/settings", "/api/v1/logs?limit=5000", "/api/v1/audit",
                     "/api/v1/users", "/api/v1/api-keys", "/api/v1/admins", "/api/openapi.json"):
            self.assertNotIn(self.plex, self.c.get(path).text, path)
