import contextlib
import io
import logging
import unittest

from cryptography.fernet import Fernet

from tests.helpers import ADMIN, PASSWORD, AppHarness
from woosarr import admin as admin_cli
from woosarr.core import logs
from woosarr.main import create_app


class RecoveryTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def run_cli(self, *argv):
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            code = admin_cli.main(list(argv), settings=self.h.settings)
        return code, out.getvalue()

    def test_reset_password_generates_unlocks_and_signs_out(self):
        anon = self.h.new_client()
        for _ in range(5):
            anon.post("/api/v1/auth/login", json={"username": ADMIN, "password": "wrong-password-x"})
        code, out = self.run_cli("list")
        self.assertIn("locked", out)
        code, out = self.run_cli("reset-password", ADMIN, "--generate")
        self.assertEqual(code, 0, out)
        new_password = out.strip().splitlines()[-1].split(": ", 1)[1]
        self.assertEqual(self.h.client.get("/api/v1/admins").status_code, 401)  # old session ended
        self.h.app.state.login_limiter.reset()
        self.h.login(ADMIN, new_password)

    def test_unknown_user(self):
        code, _ = self.run_cli("unlock", "ghost")
        self.assertEqual(code, 1)

    def test_lost_master_key_does_not_stop_startup(self):
        with self.h.app.state.db.session() as s:
            self.h.app.state.secrets.set(s, "plex.token", "x" * 20)
        (self.h.settings.config_dir / "secrets" / "master.key").write_bytes(Fernet.generate_key())
        app2 = create_app(self.h.settings)  # must not raise
        try:
            entries, _ = logs.buffer().query(min_level=logging.ERROR)
            self.assertTrue(any("can't be decrypted" in e.message for e in entries))
            with app2.state.db.session() as s:
                self.assertFalse(app2.state.secrets.readable(s, "plex.token"))
        finally:
            app2.state.db.engine.dispose()
        self.h.login(ADMIN, PASSWORD)
