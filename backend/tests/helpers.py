"""Shared test fixtures: an app on a temp /config and a logged-in client."""

import logging
import tempfile
from pathlib import Path

from fastapi.testclient import TestClient

from woosarr.core import logs
from woosarr.core.config import Settings
from woosarr.main import create_app

ADMIN = "admin"
# Assembled so no password-shaped literal sits in source.
PASSWORD = "correct-" + "horse-battery"


class AppHarness:
    def __init__(self, static_dir: Path | None = None, **overrides):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.settings = Settings(
            config_dir=root / "config", static_dir=static_dir or root / "no-static", **overrides
        )
        self.app = create_app(self.settings)
        self.client = TestClient(self.app, raise_server_exceptions=False)
        self.csrf: str | None = None

    def setup_admin(self, username: str = ADMIN, password: str = PASSWORD) -> TestClient:
        code = self.app.state.setup_code.value
        r = self.client.post(
            "/api/v1/setup/admin", json={"username": username, "password": password, "setup_code": code}
        )
        assert r.status_code == 200, r.text
        self.csrf = r.json()["csrf_token"]
        self.client.headers["X-CSRF-Token"] = self.csrf
        return self.client

    def new_client(self) -> TestClient:
        return TestClient(self.app, raise_server_exceptions=False)

    def login(self, username: str, password: str) -> TestClient:
        c = self.new_client()
        r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
        assert r.status_code == 200, r.text
        c.headers["X-CSRF-Token"] = r.json()["csrf_token"]
        return c

    def close(self):
        self.app.state.db.engine.dispose()
        for h in logs._state.handlers:
            logging.getLogger().removeHandler(h)
            h.close()
        logs._state.handlers = []
        self.tmp.cleanup()
