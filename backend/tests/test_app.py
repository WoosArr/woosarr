import logging
import tempfile
import unittest
from pathlib import Path

from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from woosarr.api.deps import PUBLIC_PATHS, require_admin
from woosarr.core import logs
from woosarr.core.config import Settings
from woosarr.main import create_app


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        static = root / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html>spa</html>")
        (static / "assets" / "app.js").write_text("console.log(1)")
        (root / "outside.txt").write_text("nope")
        self.settings = Settings(config_dir=root / "config", static_dir=static)
        self.app = create_app(self.settings)

        @self.app.get("/api/v1/_boom", include_in_schema=False)
        def boom():
            raise RuntimeError("internal detail")

        # Ahead of the SPA catch-all, like real routes registered in create_app.
        self.app.router.routes.insert(0, self.app.router.routes.pop())

        self.client = TestClient(self.app, raise_server_exceptions=False)

    def tearDown(self):
        for h in logs._state.handlers:
            logging.getLogger().removeHandler(h)
            h.close()
        logs._state.handlers = []
        self.tmp.cleanup()

    def test_health_versioned_and_alias(self):
        for path in ("/api/health", "/api/v1/health"):
            r = self.client.get(path)
            self.assertEqual(r.status_code, 200, path)
            self.assertEqual(r.json()["status"], "ok")
            self.assertEqual(r.json()["api"], "v1")
            self.assertIn("X-Request-ID", r.headers)

    def test_unhandled_error_hides_details_and_is_logged(self):
        r = self.client.get("/api/v1/_boom")
        self.assertEqual(r.status_code, 500)
        body = r.json()
        self.assertEqual(body["error"], "internal_error")
        self.assertNotIn("internal detail", r.text)
        page = self.client.get("/api/v1/logs", params={"level": "ERROR"}).json()
        hits = [e for e in page["entries"] if body["request_id"] in e["message"]]
        self.assertEqual(len(hits), 1)
        self.assertIn("RuntimeError: internal detail", hits[0]["exc"])

    def test_logs_cursor_polling(self):
        logging.getLogger("woosarr.test").info("hello from test")
        page = self.client.get("/api/v1/logs", params={"q": "hello from test"}).json()
        self.assertEqual(len(page["entries"]), 1)
        later = self.client.get("/api/v1/logs", params={"since": page["latest_seq"], "q": "hello from test"})
        self.assertEqual(later.json()["entries"], [])

    def test_logs_rejects_bad_params(self):
        self.assertEqual(self.client.get("/api/v1/logs", params={"level": "LOUD"}).status_code, 422)
        self.assertEqual(self.client.get("/api/v1/logs", params={"limit": 0}).status_code, 422)

    def test_log_file_download(self):
        logging.getLogger("woosarr.test").info("persisted line")
        for h in logs._state.handlers:
            h.flush()
        r = self.client.get("/api/v1/logs/file")
        self.assertEqual(r.status_code, 200)
        self.assertIn("persisted line", r.text)

    def test_unknown_api_path_is_404_not_spa(self):
        self.assertEqual(self.client.get("/api/v1/nope").status_code, 404)
        self.assertEqual(self.client.get("/api").status_code, 404)

    def test_spa_serving_and_traversal(self):
        self.assertIn("spa", self.client.get("/").text)
        self.assertIn("spa", self.client.get("/users/42").text)
        self.assertIn("console", self.client.get("/assets/app.js").text)
        self.assertNotIn("nope", self.client.get("/../outside.txt").text)
        self.assertNotIn("nope", self.client.get("/%2e%2e/outside.txt").text)

    def test_every_non_public_route_requires_admin(self):
        # N10: adding a route without the auth dependency must fail this test.
        for route in self.app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
                continue
            if route.path in PUBLIC_PATHS or route.path == "/api/v1/_boom":
                continue
            deps = {d.call for d in route.dependant.dependencies}
            self.assertIn(require_admin, deps, route.path)
