import unittest

from tests.fake_plextv import MACHINE, FakePlexTV
from tests.helpers import AppHarness
from tests.test_integrations import OWNER, FakeAccounts, FakePin
from woosarr.services.plex import PlexService


class IntegrationApiTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()
        self.tv = FakePlexTV()
        self.accounts = FakeAccounts(self.tv)
        FakePin.instances = []
        app = self.h.app
        svc = PlexService(app.state.db, app.state.secrets, pin_factory=FakePin, account_factory=self.accounts)
        app.state.plex = svc
        app.state.plex_status = svc.status
        app.state.sync._gateway = svc.gateway

    def tearDown(self):
        self.h.close()

    def test_status_before_setup(self):
        self.assertFalse(self.c.get("/api/v1/plex/status").json()["signed_in"])
        d = self.c.get("/api/v1/discord/status").json()
        self.assertFalse(d["configured"])
        overall = self.c.get("/api/v1/status").json()
        self.assertEqual(overall["plex"]["detail"], "Not signed in to Plex")

    def test_plex_oauth_flow_over_http(self):
        start = self.c.post("/api/v1/plex/login").json()
        self.assertTrue(start["auth_url"].startswith("https://app.plex.tv/auth/"))
        self.assertIn("forwardUrl=http://testserver/setup", start["auth_url"])
        poll = self.c.post("/api/v1/plex/login/poll", json={"pin_id": start["pin_id"]}).json()
        self.assertEqual(poll["state"], "pending")
        FakePin.instances[-1].token = OWNER
        poll = self.c.post("/api/v1/plex/login/poll", json={"pin_id": start["pin_id"]}).json()
        self.assertEqual(poll["state"], "done")
        self.assertEqual(poll["account"]["username"], "owner")
        servers = self.c.get("/api/v1/plex/servers").json()
        self.assertEqual({s["machine_id"] for s in servers}, {MACHINE})
        self.assertEqual(self.c.put("/api/v1/plex/server", json={"machine_id": "friend-srv"}).status_code, 400)
        st = self.c.put("/api/v1/plex/server", json={"machine_id": MACHINE}).json()
        self.assertTrue(st["configured"])
        check = self.c.post("/api/v1/plex/check/read").json()
        self.assertEqual(check["users"], 3)
        # the owner token never appears in any response or the audit trail
        for path in ("/api/v1/plex/status", "/api/v1/status", "/api/v1/audit", "/api/v1/logs?limit=5000"):
            self.assertNotIn(OWNER, self.c.get(path).text, path)

    def test_server_info(self):
        self.assertIsNone(self.c.get("/api/v1/plex/server").json())
        self.h.app.state.plex.complete_owner_login(OWNER)
        self.h.app.state.plex.select_server(MACHINE)
        info = self.c.get("/api/v1/plex/server").json()
        self.assertEqual((info["name"], info["version"], info["online"]), ("Home", "1.41", True))

    def test_write_check_requires_confirmation(self):
        r = self.c.post("/api/v1/plex/check/write", json={"plex_user_id": "11"})
        self.assertEqual(r.status_code, 422)

    def test_not_signed_in_is_a_409(self):
        self.assertEqual(self.c.get("/api/v1/plex/servers").status_code, 409)
        self.assertEqual(self.c.post("/api/v1/plex/check/read").status_code, 409)

    def test_bad_discord_token_rejected_without_storing(self):
        r = self.c.put("/api/v1/discord/token", json={"token": "x" * 30 + " spaces"})
        self.assertEqual(r.status_code, 400)
        self.assertFalse(self.c.get("/api/v1/discord/status").json()["configured"])


class DemoModeIntegrationTests(unittest.TestCase):
    def test_setup_disabled_in_demo(self):
        h = AppHarness(demo=True)
        try:
            c = h.setup_admin()
            self.assertEqual(c.post("/api/v1/plex/login").status_code, 409)
            self.assertEqual(c.put("/api/v1/discord/token", json={"token": "t" * 60}).status_code, 409)
            self.assertIn("Demo mode", c.get("/api/v1/discord/status").json()["detail"])
            self.assertIn("Demo mode", c.get("/api/v1/plex/status").json()["detail"])
        finally:
            h.close()

