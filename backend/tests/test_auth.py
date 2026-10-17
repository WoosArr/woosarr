import logging
import threading
import unittest
from datetime import timedelta

from sqlalchemy import select

from tests.helpers import ADMIN, PASSWORD, AppHarness
from woosarr.core import auth
from woosarr.db.models import AuditLog, utcnow
from woosarr.db.models import Session as DbSession

OTHER_PASSWORD = "another-" + "long-password"


class FirstRunSetupTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.client

    def tearDown(self):
        self.h.close()

    def test_status_reports_setup_required(self):
        body = self.c.get("/api/v1/auth/status").json()
        self.assertTrue(body["setup_required"])
        self.assertTrue(body["setup_code_required"])
        self.assertFalse(body["authenticated"])

    def test_setup_needs_the_code(self):
        for code in (None, "0000-0000-0000"):
            r = self.c.post("/api/v1/setup/admin", json={"username": ADMIN, "password": PASSWORD, "setup_code": code})
            self.assertEqual(r.status_code, 403)

    def test_code_is_case_and_dash_insensitive(self):
        code = self.h.app.state.setup_code.value.replace("-", "").upper()
        r = self.c.post("/api/v1/setup/admin", json={"username": ADMIN, "password": PASSWORD, "setup_code": code})
        self.assertEqual(r.status_code, 200, r.text)

    def test_weak_password_rejected(self):
        code = self.h.app.state.setup_code.value
        r = self.c.post("/api/v1/setup/admin", json={"username": ADMIN, "password": "short", "setup_code": code})
        self.assertEqual(r.status_code, 422)

    def test_setup_is_one_shot(self):
        code = self.h.app.state.setup_code.value
        self.h.setup_admin()
        r = self.h.new_client().post(
            "/api/v1/setup/admin", json={"username": "intruder", "password": PASSWORD, "setup_code": code}
        )
        self.assertEqual(r.status_code, 409)
        self.assertFalse(self.c.get("/api/v1/auth/status").json()["setup_required"])
        self.assertIsNone(self.h.app.state.setup_code)

    def test_concurrent_claims_create_exactly_one_admin(self):
        code = self.h.app.state.setup_code.value
        results = []

        def claim(i):
            r = self.h.new_client().post(
                "/api/v1/setup/admin", json={"username": f"admin{i}", "password": PASSWORD, "setup_code": code}
            )
            results.append(r.status_code)

        threads = [threading.Thread(target=claim, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(results.count(200), 1, results)
        with self.h.app.state.db.session() as s:
            self.assertEqual(len(s.scalars(select(auth.AdminUser)).all()), 1)

    def test_setup_code_never_logged(self):
        code = self.h.app.state.setup_code.value
        self.h.setup_admin()
        page = self.c.get("/api/v1/logs", params={"limit": 5000}).text
        self.assertNotIn(code, page)
        self.assertNotIn(code, self.h.settings.log_file.read_text())


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def test_unauthenticated_requests_rejected(self):
        anon = self.h.new_client()
        for path in ("/api/v1/logs", "/api/v1/admins", "/api/v1/api-keys", "/api/openapi.json", "/api/docs"):
            self.assertEqual(anon.get(path).status_code, 401, path)

    def test_cookie_flags(self):
        r = self.h.new_client().post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
        cookie = r.headers["set-cookie"].lower()
        self.assertIn("httponly", cookie)
        self.assertIn("samesite=lax", cookie)
        self.assertNotIn("secure", cookie)  # plain HTTP test client, cookie_secure=auto

    def test_secure_cookie_when_forced(self):
        h = AppHarness(cookie_secure="always")
        try:
            h.setup_admin()
            r = h.new_client().post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
            self.assertIn("secure", r.headers["set-cookie"].lower())
        finally:
            h.close()

    def test_csrf_required_for_unsafe_methods(self):
        body = {"username": "second", "password": PASSWORD}
        no_csrf = self.h.new_client()
        no_csrf.cookies = self.c.cookies
        self.assertEqual(no_csrf.post("/api/v1/admins", json=body).status_code, 403)
        no_csrf.headers["X-CSRF-Token"] = "wrong"
        self.assertEqual(no_csrf.post("/api/v1/admins", json=body).status_code, 403)
        self.assertEqual(self.c.post("/api/v1/admins", json=body).status_code, 201)

    def test_login_rotates_session_and_logout_ends_it(self):
        old_cookie = self.c.cookies.get(auth.SESSION_COOKIE)
        c2 = self.h.login(ADMIN, PASSWORD)
        self.assertNotEqual(c2.cookies.get(auth.SESSION_COOKIE), old_cookie)
        self.assertEqual(c2.post("/api/v1/auth/logout").status_code, 200)
        stale = self.h.new_client()
        stale.cookies.set(auth.SESSION_COOKIE, c2.cookies.get(auth.SESSION_COOKIE) or "x")
        self.assertEqual(stale.get("/api/v1/admins").status_code, 401)

    def test_idle_session_expires(self):
        with self.h.app.state.db.session() as s:
            for row in s.scalars(select(DbSession)):
                row.last_seen_at = utcnow() - timedelta(hours=13)
        self.assertEqual(self.c.get("/api/v1/admins").status_code, 401)

    def test_wrong_password_and_lockout(self):
        anon = self.h.new_client()
        for _ in range(auth.MAX_FAILED_LOGINS):
            r = anon.post("/api/v1/auth/login", json={"username": ADMIN, "password": "wrong-password-x"})
            self.assertEqual(r.status_code, 401)
        r = anon.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD})
        self.assertEqual(r.status_code, 401)
        self.assertIn("locked", r.json()["detail"])
        admins = self.c.get("/api/v1/admins").json()
        self.assertTrue(admins[0]["locked"])
        self.c.patch(f"/api/v1/admins/{admins[0]['id']}", json={"unlock": True})
        self.h.app.state.login_limiter.reset()
        self.assertEqual(anon.post("/api/v1/auth/login", json={"username": ADMIN, "password": PASSWORD}).status_code, 200)

    def test_unknown_user_gets_same_error(self):
        r = self.h.new_client().post("/api/v1/auth/login", json={"username": "nobody", "password": PASSWORD})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.json()["detail"], "invalid username or password")

    def test_ip_rate_limit(self):
        anon = self.h.new_client()
        codes = [
            anon.post("/api/v1/auth/login", json={"username": f"u{i}x", "password": "nope-nope-nope"}).status_code
            for i in range(12)
        ]
        self.assertIn(429, codes)

    def test_password_change_ends_other_sessions(self):
        other = self.h.login(ADMIN, PASSWORD)
        r = self.c.post("/api/v1/auth/password", json={"current_password": PASSWORD, "new_password": OTHER_PASSWORD})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(other.get("/api/v1/admins").status_code, 401)
        self.assertEqual(self.c.get("/api/v1/admins").status_code, 200)
        self.h.login(ADMIN, OTHER_PASSWORD)

    def test_audit_records_login(self):
        self.h.login(ADMIN, PASSWORD)
        with self.h.app.state.db.session() as s:
            actions = [a.action for a in s.scalars(select(AuditLog))]
        self.assertIn("first_admin_created", actions)
        self.assertIn("login", actions)


class AdminManagementTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.me = self.c.get("/api/v1/auth/status").json()["user"]["id"]

    def tearDown(self):
        self.h.close()

    def test_last_admin_cannot_be_removed_or_disabled(self):
        self.assertEqual(self.c.delete(f"/api/v1/admins/{self.me}").status_code, 409)
        self.assertEqual(self.c.patch(f"/api/v1/admins/{self.me}", json={"is_active": False}).status_code, 409)

    def test_create_disable_delete(self):
        r = self.c.post("/api/v1/admins", json={"username": "second", "password": PASSWORD})
        self.assertEqual(r.status_code, 201)
        second = r.json()["id"]
        self.assertEqual(self.c.post("/api/v1/admins", json={"username": "second", "password": PASSWORD}).status_code, 409)
        c2 = self.h.login("second", PASSWORD)
        self.assertEqual(self.c.patch(f"/api/v1/admins/{second}", json={"is_active": False}).status_code, 200)
        self.assertEqual(c2.get("/api/v1/admins").status_code, 401)
        self.assertEqual(self.c.delete(f"/api/v1/admins/{second}").status_code, 200)
        # With the other admin gone, self is last again.
        self.assertEqual(self.c.delete(f"/api/v1/admins/{self.me}").status_code, 409)

    def test_bad_username_rejected(self):
        r = self.c.post("/api/v1/admins", json={"username": "<script>", "password": PASSWORD})
        self.assertEqual(r.status_code, 422)


class ApiKeyTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def _key(self, scope="read"):
        r = self.c.post("/api/v1/api-keys", json={"name": "diag", "scope": scope})
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def test_read_key_can_read_logs_but_not_write(self):
        key = self._key()["key"]
        bearer = self.h.new_client()
        bearer.headers["Authorization"] = f"Bearer {key}"
        self.assertEqual(bearer.get("/api/v1/logs").status_code, 200)
        r = bearer.post("/api/v1/admins", json={"username": "x-user", "password": PASSWORD})
        self.assertEqual(r.status_code, 403)

    def test_admin_key_writes_without_csrf_but_cannot_mint_keys(self):
        key = self._key("admin")["key"]
        bearer = self.h.new_client()
        bearer.headers["Authorization"] = f"Bearer {key}"
        self.assertEqual(bearer.post("/api/v1/admins", json={"username": "x-user", "password": PASSWORD}).status_code, 201)
        self.assertEqual(bearer.post("/api/v1/api-keys", json={"name": "n"}).status_code, 403)

    def test_revoked_and_bogus_keys_rejected(self):
        created = self._key()
        self.c.delete(f"/api/v1/api-keys/{created['id']}")
        for value in (created["key"], "wsk_deadbeef_" + "x" * 30, "garbage"):
            bearer = self.h.new_client()
            bearer.headers["Authorization"] = f"Bearer {value}"
            self.assertEqual(bearer.get("/api/v1/logs").status_code, 401, value)

    def test_key_shown_once_and_never_logged(self):
        created = self._key()
        listing = self.c.get("/api/v1/api-keys").text
        self.assertNotIn(created["key"], listing)
        self.assertIn(created["prefix"], listing)
        logging.getLogger("woosarr.test").warning("oops leaked %s", created["key"])
        self.assertNotIn(created["key"], self.c.get("/api/v1/logs", params={"limit": 5000}).text)
