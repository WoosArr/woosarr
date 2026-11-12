import logging
import tempfile
import unittest
from pathlib import Path

from fastapi.routing import APIRoute

from tests.helpers import AppHarness
from woosarr.api.deps import PUBLIC_PATHS, require_admin
from woosarr.core import logs


class AppTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        static = root / "static"
        (static / "assets").mkdir(parents=True)
        (static / "index.html").write_text("<html>spa</html>")
        (static / "assets" / "app.js").write_text("console.log(1)")
        (static / "logo-64.webp").write_bytes(b"RIFF\x00\x00\x00\x00WEBP")
        (static / "site.webmanifest").write_text('{"name": "WooSarr"}')
        (root / "outside.txt").write_text("nope")
        self.h = AppHarness(static_dir=static)
        self.app = self.h.app

        @self.app.get("/api/v1/_boom", include_in_schema=False)
        def boom():
            raise RuntimeError("internal detail")

        # Ahead of the SPA catch-all, like real routes registered in create_app.
        self.app.router.routes.insert(0, self.app.router.routes.pop())
        self.client = self.h.setup_admin()

    def tearDown(self):
        self.h.close()
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
        logs.flush()
        r = self.client.get("/api/v1/logs/file")
        self.assertEqual(r.status_code, 200)
        self.assertIn("persisted line", r.text)
        logging.getLogger("woosarr.test").warning("lasting warning")
        logs.flush()
        r = self.client.get("/api/v1/logs/file", params={"name": "alerts"})
        self.assertIn("lasting warning", r.text)
        self.assertNotIn("persisted line", r.text)
        self.assertIn('filename="alerts.log"', r.headers["content-disposition"])
        self.assertEqual(self.client.get("/api/v1/logs/file", params={"name": "../app.db"}).status_code, 422)

    def test_unknown_api_path_is_404_not_spa(self):
        self.assertEqual(self.client.get("/api/v1/nope").status_code, 404)
        self.assertEqual(self.client.get("/api").status_code, 404)

    def test_spa_serving_and_traversal(self):
        self.assertIn("spa", self.client.get("/").text)
        self.assertIn("spa", self.client.get("/users/42").text)
        self.assertIn("console", self.client.get("/assets/app.js").text)
        self.assertNotIn("nope", self.client.get("/../outside.txt").text)
        self.assertNotIn("nope", self.client.get("/%2e%2e/outside.txt").text)

    def test_icon_and_manifest_content_types(self):
        self.assertEqual(self.client.get("/logo-64.webp").headers["content-type"], "image/webp")
        self.assertEqual(self.client.get("/site.webmanifest").headers["content-type"], "application/manifest+json")

    def test_missing_files_404_instead_of_spa(self):
        self.assertEqual(self.client.get("/favicon.ico").status_code, 404)
        self.assertEqual(self.client.get("/assets/missing.js").status_code, 404)
        self.assertEqual(self.client.get("/settings/theme").status_code, 200)

    def test_security_and_cache_headers(self):
        r = self.client.get("/")
        self.assertEqual(r.headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")
        self.assertIn("frame-ancestors 'none'", r.headers["Content-Security-Policy"])
        self.assertEqual(r.headers["Cache-Control"], "no-cache")
        self.assertIn("immutable", self.client.get("/assets/app.js").headers["Cache-Control"])
        self.assertEqual(self.client.get("/api/health").headers["Cache-Control"], "no-store")
        self.assertNotIn("Content-Security-Policy", self.client.get("/api/docs").headers)

    def test_request_id_is_validated(self):
        ok = self.client.get("/api/health", headers={"X-Request-ID": "abc-123.x_y"})
        self.assertEqual(ok.headers["X-Request-ID"], "abc-123.x_y")
        for bad in ("<script>", "a" * 65, "id with spaces"):
            r = self.client.get("/api/health", headers={"X-Request-ID": bad})
            self.assertNotEqual(r.headers["X-Request-ID"], bad)
            self.assertRegex(r.headers["X-Request-ID"], r"^[0-9a-f]{12}$")

    def test_db_session_commits_before_response(self):
        # Regression: login returned 200 before its session row was committed,
        # so the browser's next request was sometimes unauthenticated.
        from typing import get_args

        from woosarr.api.deps import DbDep

        depends = get_args(DbDep)[1]
        self.assertEqual(depends.scope, "function")

    def test_every_non_public_route_requires_admin(self):
        # N10: adding a route without the auth dependency must fail this test.
        for route in self.app.routes:
            if not isinstance(route, APIRoute) or not route.path.startswith("/api"):
                continue
            if route.path in PUBLIC_PATHS or route.path == "/api/v1/_boom":
                continue
            deps = {d.call for d in route.dependant.dependencies}
            self.assertIn(require_admin, deps, route.path)


class PreloadTests(unittest.TestCase):
    def test_file_serving_helpers_are_imported_before_the_loop_runs(self):
        # Live 2026-10-23: anyio imported its file module on the first static file served, inside
        # the event loop; a cold disk made that take >10 s and stalled the Discord heartbeat.
        import subprocess
        import sys

        code = ("import sys, tempfile, pathlib; from woosarr.core.config import Settings; "
                "from woosarr.main import create_app; d = pathlib.Path(tempfile.mkdtemp()); "
                "create_app(Settings(config_dir=d / 'c', static_dir=d / 's')); "
                "print('anyio._core._fileio' in sys.modules)")
        out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False,
                             cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(out.stdout.strip().splitlines()[-1], "True", out.stderr[-2000:])
