"""OIDC admin sign-in (D21), end to end against a fake provider."""

import time
import unittest
from typing import ClassVar
from unittest import mock
from urllib.parse import unquote

from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy import select

from tests.fake_idp import FakeIdP, query
from tests.helpers import AppHarness
from woosarr.db.models import AdminUser, AuditLog, OidcIdentity
from woosarr.services.oidc import OidcService


class Base(unittest.TestCase):
    kind = "custom"
    settings: ClassVar[dict[str, str]] = {"issuer": "https://idp.test"}

    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.idp = self.make_idp()
        self.h.app.state.oidc = OidcService(http=self.idp)
        self.provider = self.create(enabled=True, allowed_groups=["WooSarr Admins"])

    def tearDown(self):
        self.h.close()

    def make_idp(self):
        return FakeIdP()

    def create(self, **overrides):
        body = {"name": "Test IdP", "kind": self.kind, "settings": self.settings, "client_id": self.idp.client_id,
                "client_secret": self.idp.client_secret, **overrides}
        r = self.c.post("/api/v1/oidc/providers", json=body)
        self.assertEqual(r.status_code, 201, r.text)
        return r.json()

    def sign_in(self, browser=None, slug=None, tamper=None):
        """Run the browser flow; returns the final callback response."""
        b = browser or self.h.new_client()
        r = b.get(f"/api/v1/auth/oidc/{slug or self.provider['slug']}/login", follow_redirects=False)
        self.assertEqual(r.status_code, 303, r.text)
        url = r.headers["location"]
        if not url.startswith("https://"):
            return r  # failed before leaving WooSarr
        code = self.idp.authorize(url)
        state = query(url)["state"]
        params = {"code": code, "state": state}
        if tamper:
            tamper(b, params)
        return b.get("/api/v1/auth/oidc/callback", params=params, follow_redirects=False)


class SignInTests(Base):
    def test_full_sign_in_creates_and_reuses_admin(self):
        b = self.h.new_client()
        r = self.sign_in(b)
        self.assertEqual((r.status_code, r.headers["location"]), (303, "/"))
        status = b.get("/api/v1/auth/status").json()
        self.assertTrue(status["authenticated"])
        self.assertEqual(status["user"]["username"], "alice")
        self.assertEqual(status["user"]["auth_source"], "oidc")
        # Same subject, changed email/username at the IdP -> same admin (linked by sub, not email)
        self.idp.claims.update(email="new@example.com", preferred_username="alice2")
        self.sign_in(self.h.new_client())
        with self.h.app.state.db.session() as s:
            self.assertEqual(len(s.scalars(select(AdminUser).where(AdminUser.auth_source == "oidc")).all()), 1)
            self.assertEqual(s.scalars(select(OidcIdentity.email)).one(), "new@example.com")

    def test_pkce_and_secret_are_sent(self):
        self.sign_in()
        post = [c for c in self.idp.calls if c[0] == "POST"][-1]
        self.assertIn("code_verifier", post[2])
        self.assertEqual(post[3], (self.idp.client_id, self.idp.client_secret))  # client_secret_basic

    def test_client_secret_post_when_basic_unsupported(self):
        self.idp.auth_methods = ["client_secret_post"]
        self.h.app.state.oidc = OidcService(http=self.idp)
        self.sign_in()
        post = [c for c in self.idp.calls if c[0] == "POST"][-1]
        self.assertIsNone(post[3])
        self.assertEqual(post[2]["client_secret"], self.idp.client_secret)

    def test_state_cookie_required(self):
        def drop_cookie(b, _params):
            b.cookies.clear()
        r = self.sign_in(tamper=drop_cookie)
        self.assertIn("sso_error", r.headers["location"])
        self.assertIn("state", r.headers["location"])

    def test_state_is_single_use(self):
        b = self.h.new_client()
        r = b.get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", follow_redirects=False)
        url = r.headers["location"]
        code, state = self.idp.authorize(url), query(url)["state"]
        first = b.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
        self.assertEqual(first.headers["location"], "/")
        b.cookies.set("woosarr_oidc_state", state, path="/api/v1/auth/oidc")
        again = b.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
        self.assertIn("sso_error", again.headers["location"])

    def _expect_error(self, fragment):
        r = self.sign_in()
        loc = unquote(r.headers["location"])
        self.assertIn("sso_error", loc)
        self.assertIn(fragment, loc)
        with self.h.app.state.db.session() as s:
            self.assertIn("login_failed", s.scalars(select(AuditLog.action)).all())

    def test_wrong_nonce(self):
        self.idp.token_overrides = {"nonce": "attacker"}
        self._expect_error("nonce")

    def test_expired_token(self):
        now = int(time.time())
        self.idp.token_overrides = {"iat": now - 3600, "exp": now - 1800}
        self._expect_error("expired")

    def test_wrong_audience(self):
        self.idp.token_overrides = {"aud": "someone-else"}
        self._expect_error("different application")

    def test_multiple_audiences_need_azp(self):
        self.idp.token_overrides = {"aud": [self.idp.client_id, "other"], "azp": "other"}
        self._expect_error("azp")

    def test_wrong_issuer(self):
        self.idp.token_overrides = {"iss": "https://evil.test"}
        self._expect_error("different issuer")

    def test_unsigned_and_shared_secret_tokens_rejected(self):
        self.idp.sign_with = ("", "none", None)
        self._expect_error("unexpected algorithm")
        self.idp.sign_with = ("k" * 32, "HS256", "k1")
        self._expect_error("unexpected algorithm")

    def test_token_signed_by_another_key(self):
        other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.idp.sign_with = (other, "RS256", "k1")
        self._expect_error("invalid")
        self.idp.sign_with = (other, "RS256", "unknown-kid")
        self._expect_error("unknown key")

    def test_not_in_allowed_group(self):
        self.idp.claims["groups"] = ["Everyone"]
        self._expect_error("isn't allowed")

    def test_groups_from_userinfo(self):
        self.idp.claims.pop("groups")
        self.idp.userinfo = {"groups": ["WooSarr Admins"]}
        self.assertEqual(self.sign_in().headers["location"], "/")

    def test_disabled_admin_refused(self):
        self.sign_in()
        with self.h.app.state.db.session() as s:
            s.scalar(select(AdminUser).where(AdminUser.auth_source == "oidc")).is_active = False
        self._expect_error("disabled")

    def test_idp_error_is_shown(self):
        b = self.h.new_client()
        r = b.get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", follow_redirects=False)
        state = query(r.headers["location"])["state"]
        r = b.get("/api/v1/auth/oidc/callback", params={"state": state, "error": "access_denied",
                                                       "error_description": "User cancelled"}, follow_redirects=False)
        self.assertIn("User cancelled", unquote(r.headers["location"]))

    def test_next_path_is_local_only(self):
        b = self.h.new_client()
        for nxt, expected in (("/users", "/users"), ("//evil.example", "/"), ("https://evil.example", "/")):
            r = b.get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", params={"next": nxt}, follow_redirects=False)
            url = r.headers["location"]
            code, state = self.idp.authorize(url), query(url)["state"]
            done = b.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
            self.assertEqual(done.headers["location"], expected, nxt)

    def test_disabled_provider_and_unknown_slug(self):
        self.c.put(f"/api/v1/oidc/providers/{self.provider['id']}", json={
            "name": "Test IdP", "kind": "custom", "settings": self.settings, "client_id": self.idp.client_id,
            "allowed_groups": ["WooSarr Admins"], "enabled": False})
        r = self.h.new_client().get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", follow_redirects=False)
        self.assertIn("sso_error", r.headers["location"])
        self.assertEqual(self.h.new_client().get("/api/v1/auth/oidc/providers").json(), [])


class AccessRuleTests(Base):
    def test_rules_required_to_enable(self):
        r = self.c.post("/api/v1/oidc/providers", json={"name": "No rules", "kind": "custom", "settings": self.settings,
                                                        "client_id": "x", "enabled": True})
        self.assertEqual(r.status_code, 422)

    def test_email_must_be_verified(self):
        self.c.put(f"/api/v1/oidc/providers/{self.provider['id']}", json={
            "name": "Test IdP", "kind": "custom", "settings": self.settings, "client_id": self.idp.client_id,
            "allowed_emails": ["admin@example.com"], "enabled": True})
        self.idp.claims["email_verified"] = False
        self.assertIn("verified", unquote(self.sign_in().headers["location"]))
        self.idp.claims["email_verified"] = "true"  # some IdPs send strings
        self.assertEqual(self.sign_in().headers["location"], "/")

    def test_domain_rule(self):
        self.c.put(f"/api/v1/oidc/providers/{self.provider['id']}", json={
            "name": "Test IdP", "kind": "custom", "settings": self.settings, "client_id": self.idp.client_id,
            "allowed_domains": ["@Example.com"], "enabled": True})
        self.assertEqual(self.sign_in().headers["location"], "/")
        self.idp.claims["email"] = "x@other.com"
        self.assertIn("sso_error", self.sign_in().headers["location"])


class GoogleTests(Base):
    kind = "google"
    settings: ClassVar[dict[str, str]] = {"hosted_domain": "example.com"}

    def make_idp(self):
        return FakeIdP(issuer="https://accounts.google.com")

    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.idp = self.make_idp()
        self.h.app.state.oidc = OidcService(http=self.idp)
        self.provider = self.create(enabled=True, allowed_domains=["example.com"])

    def test_hd_claim_decides_domain_and_hint_is_sent(self):
        b = self.h.new_client()
        r = b.get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", follow_redirects=False)
        q = query(r.headers["location"])
        self.assertEqual((q["hd"], q["prompt"]), ("example.com", "select_account"))
        self.assertIn("sso_error", self.sign_in().headers["location"])  # no hd claim: personal account
        self.idp.claims["hd"] = "example.com"
        self.assertEqual(self.sign_in().headers["location"], "/")

    def test_groups_not_allowed_for_google(self):
        r = self.c.post("/api/v1/oidc/providers", json={"name": "G2", "kind": "google", "settings": {},
                                                        "client_id": "x", "allowed_groups": ["a"]})
        self.assertEqual(r.status_code, 422)


class EntraTests(Base):
    kind = "entra"
    settings: ClassVar[dict[str, str]] = {"tenant": "contoso.onmicrosoft.com"}
    guid = "11111111-2222-3333-4444-555555555555"

    def make_idp(self):
        return FakeIdP(issuer="https://login.microsoftonline.com/contoso.onmicrosoft.com/v2.0",
                       discovered_issuer=f"https://login.microsoftonline.com/{self.guid}/v2.0")

    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.idp = self.make_idp()
        self.idp.claims = {"sub": "entra-sub", "preferred_username": "admin@contoso.com", "roles": ["WooSarr.Admin"]}
        self.h.app.state.oidc = OidcService(http=self.idp)
        self.provider = self.create(enabled=True, allowed_groups=["WooSarr.Admin"])

    def test_domain_tenant_resolves_to_guid_issuer(self):
        self.assertEqual(self.sign_in().headers["location"], "/")  # roles claim, no email_verified needed
        d = self.c.post(f"/api/v1/oidc/providers/{self.provider['id']}/discover").json()
        self.assertEqual(d["issuer"], f"https://login.microsoftonline.com/{self.guid}/v2.0")

    def test_multi_tenant_issuers_refused(self):
        r = self.c.post("/api/v1/oidc/providers", json={"name": "E2", "kind": "entra", "settings": {"tenant": "common"},
                                                        "client_id": "x"})
        self.assertEqual(r.status_code, 422)
        self.idp.discovered_issuer = "https://login.microsoftonline.com/{tenantid}/v2.0"
        self.h.app.state.oidc = OidcService(http=self.idp)
        d = self.c.post(f"/api/v1/oidc/providers/{self.provider['id']}/discover").json()
        self.assertFalse(d["ok"])
        self.assertIn("issuer mismatch", d["error"])


class AdminApiTests(Base):
    def test_presets_and_redirect_uri(self):
        p = self.c.get("/api/v1/oidc/presets").json()
        self.assertEqual({x["kind"] for x in p["presets"]}, {"okta", "entra", "google", "adfs", "custom"})
        self.assertEqual(p["redirect_uri"], "http://testserver/api/v1/auth/oidc/callback")
        self.assertFalse(p["https"])
        self.c.patch("/api/v1/settings", json={"public_url": "https://woosarr.example.com"})
        p = self.c.get("/api/v1/oidc/presets").json()
        self.assertEqual(p["redirect_uri"], "https://woosarr.example.com/api/v1/auth/oidc/callback")
        okta = next(x for x in p["presets"] if x["kind"] == "okta")
        self.assertTrue(any("{redirect_uri}" in s for s in okta["steps"]))

    def test_okta_issuer_and_scopes(self):
        r = self.c.post("/api/v1/oidc/providers", json={"name": "Okta", "kind": "okta", "client_id": "x",
                                                        "settings": {"domain": "dev-1.okta.com", "auth_server": "default"}})
        self.assertEqual(r.json()["issuer"], "https://dev-1.okta.com/oauth2/default")
        r = self.c.post("/api/v1/oidc/providers", json={"name": "Okta org", "kind": "okta", "client_id": "x",
                                                        "settings": {"domain": "dev-1.okta.com", "auth_server": "org"}})
        self.assertEqual(r.json()["issuer"], "https://dev-1.okta.com")
        bad = self.c.post("/api/v1/oidc/providers", json={"name": "Bad", "kind": "okta", "client_id": "x",
                                                          "settings": {"domain": "not a host"}})
        self.assertEqual(bad.status_code, 422)

    def test_secret_never_returned_or_logged(self):
        self.sign_in()
        for path in ("/api/v1/oidc/providers", "/api/v1/oidc/providers", "/api/v1/audit", "/api/v1/logs?limit=5000"):
            self.assertNotIn(self.idp.client_secret, self.c.get(path).text, path)
        self.assertTrue(self.c.get("/api/v1/oidc/providers").json()[0]["client_secret_set"])

    def test_test_sign_in_shows_claims_without_logging_in(self):
        r = self.c.post(f"/api/v1/oidc/providers/{self.provider['id']}/test")
        self.assertEqual(r.status_code, 200, r.text)
        url = r.json()["url"]
        code, state = self.idp.authorize(url), query(url)["state"]
        done = self.c.get("/api/v1/auth/oidc/callback", params={"code": code, "state": state}, follow_redirects=False)
        loc = done.headers["location"]
        self.assertTrue(loc.startswith("/settings?tab=sso&test="))
        result = self.c.get(f"/api/v1/oidc/test-results/{loc.rsplit('=', 1)[1]}").json()
        self.assertTrue(result["ok"])
        self.assertEqual(result["groups"], ["WooSarr Admins"])
        self.assertNotIn("nonce", result["claims"])
        with self.h.app.state.db.session() as s:
            self.assertEqual(s.scalars(select(OidcIdentity)).all(), [])  # nobody signed in

    def test_lockout_guards(self):
        r = self.c.patch("/api/v1/settings", json={"local_login_enabled": False})
        self.assertEqual(r.status_code, 409)  # no SSO admin yet
        self.sign_in()
        self.assertEqual(self.c.patch("/api/v1/settings", json={"local_login_enabled": False}).status_code, 200)
        r = self.c.put(f"/api/v1/oidc/providers/{self.provider['id']}", json={
            "name": "Test IdP", "kind": "custom", "settings": self.settings, "client_id": self.idp.client_id,
            "allowed_groups": ["WooSarr Admins"], "enabled": False})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(self.c.delete(f"/api/v1/oidc/providers/{self.provider['id']}").status_code, 409)
        login = self.h.new_client().post("/api/v1/auth/login", json={"username": "admin", "password": "x" * 12})
        self.assertEqual(login.status_code, 403)

    def test_unreachable_provider(self):
        import requests

        broken = mock.Mock()
        broken.get.side_effect = requests.ConnectionError("down")
        self.h.app.state.oidc = OidcService(http=broken)
        r = self.h.new_client().get(f"/api/v1/auth/oidc/{self.provider['slug']}/login", follow_redirects=False)
        self.assertIn("can't reach", unquote(r.headers["location"]))


class DocsTests(unittest.TestCase):
    def test_sso_docs_match_presets(self):
        from pathlib import Path

        from woosarr.services.oidc_presets import markdown

        doc = Path(__file__).resolve().parents[2] / "docs" / "sso.md"
        self.assertEqual(doc.read_text(), markdown(), "docs/sso.md is stale: run `make sso-docs`")


class SafeNextTests(unittest.TestCase):
    def test_only_same_site_paths(self):
        from woosarr.api.oidc import _safe_next

        for ok in ("/", "/settings?tab=sso", "/users#x"):
            self.assertEqual(_safe_next(ok), ok)
        for bad in (None, "", "https://evil.example", "//evil.example", "/\\evil.example", "/\t/evil.example",
                    "/\n/evil.example", "/\r/evil", "/\x7f", "x/y", "/" + "a" * 600):
            self.assertEqual(_safe_next(bad), "/", repr(bad))
