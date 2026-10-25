"""Build versions (1.2.0 / 1.3.0-dev.N), and the check for a newer build in the same channel."""

import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

import requests

from tests.helpers import AppHarness
from woosarr import __version__
from woosarr.build import Build, current
from woosarr.services.updates import UpdateChecker

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "build-info.sh"
MINE = "a" * 40
THEIRS = "b" * 40


class BuildInfoScriptTests(unittest.TestCase):
    """scripts/build-info.sh, which CI runs before building the image."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = self.tmp.name
        self.git("init", "-q", "-b", "develop")

    def tearDown(self):
        self.tmp.cleanup()

    def git(self, *args):
        env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
               "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True, env=env)

    def commit(self, n=1):
        for _ in range(n):
            self.git("commit", "-q", "--allow-empty", "-m", "x")

    def info(self, ref):
        out = subprocess.run(["sh", str(SCRIPT)], cwd=self.repo, check=True, capture_output=True, text=True,
                             env={**os.environ, "GITHUB_REF": ref}).stdout
        return dict(line.split("=", 1) for line in out.splitlines())

    def test_versions_by_channel(self):
        self.commit(2)
        self.assertEqual(self.info("refs/heads/develop")["version"], "0.1.0-dev.2")  # no release yet
        self.git("tag", "v1.4.2")
        self.git("tag", "v1.4.10")  # sorted by version, not as text
        self.git("tag", "v2.0.0-rc1")  # pre-releases don't count
        self.commit(3)
        info = self.info("refs/heads/develop")
        self.assertEqual((info["version"], info["channel"]), ("1.5.0-dev.3", "develop"))
        self.assertEqual(len(info["commit"]), 40)
        self.assertRegex(info["date"], r"^\d{4}-\d\d-\d\dT")
        self.commit()
        self.assertEqual(self.info("refs/heads/develop")["version"], "1.5.0-dev.4")  # newer = higher
        self.assertEqual(self.info("refs/tags/v1.5.0")["version"], "1.5.0")
        self.assertEqual(self.info("refs/tags/v1.5.0")["channel"], "release")
        self.assertEqual(self.info("refs/heads/main")["channel"], "dev")


class BuildTests(unittest.TestCase):
    def test_from_environment_and_labels(self):
        with mock.patch.dict(os.environ, {"WOOSARR_BUILD_VERSION": "1.2.0-dev.14", "WOOSARR_BUILD_CHANNEL": "develop",
                                          "WOOSARR_BUILD_COMMIT": "ba15809a79" + "1" * 30,
                                          "WOOSARR_BUILD_DATE": "2026-10-24T21:41:53-05:00"}):
            b = current()
        self.assertEqual(b.label(), "1.2.0-dev.14 (develop, ba15809, 24 Oct 2026)")
        with mock.patch.dict(os.environ, {}, clear=True):
            local = current()
        self.assertEqual((local.version, local.channel, local.label()), (__version__, "local",
                                                                         f"{__version__} (local build)"))


class FakeHttp:
    def __init__(self, routes):
        self.routes, self.headers, self.calls = routes, {}, []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        for prefix, body in self.routes.items():
            if url.startswith(prefix):
                if isinstance(body, int):
                    r = requests.Response()
                    r.status_code = body
                    return SimpleNamespace(raise_for_status=lambda r=r: (_ for _ in ()).throw(
                        requests.HTTPError(response=r)), json=dict)
                return SimpleNamespace(raise_for_status=lambda: None, json=lambda body=body: body)
        raise requests.ConnectionError("no route")


def develop_routes(revision, version="1.2.0-dev.30", ahead=3):
    return {
        "https://ghcr.io/token": {"token": "t"},
        "https://ghcr.io/v2/woosarr/woosarr/manifests/develop": {"manifests": [
            {"digest": "sha256:att", "platform": {"architecture": "unknown"}},
            {"digest": "sha256:amd", "platform": {"architecture": "amd64"}}]},
        "https://ghcr.io/v2/woosarr/woosarr/manifests/sha256:amd": {"config": {"digest": "sha256:cfg"}},
        "https://ghcr.io/v2/woosarr/woosarr/blobs/sha256:cfg": {"config": {"Labels": {
            "org.opencontainers.image.revision": revision, "io.woosarr.version": version}}},
        "https://api.github.com/repos/WoosArr/woosarr/compare/": {"ahead_by": ahead, "status": "ahead"},
    }


class UpdateCheckTests(unittest.TestCase):
    def checker(self, channel, version, routes):
        return UpdateChecker(Build(version, channel, MINE, "2026-10-25T00:00:00+00:00"), session=FakeHttp(routes))

    def test_release_compares_with_the_latest_release_only(self):
        latest = {"https://api.github.com/repos/WoosArr/woosarr/releases/latest":
                  {"tag_name": "v1.2.0", "html_url": "https://github.com/WoosArr/woosarr/releases/tag/v1.2.0"}}
        r = self.checker("release", "1.1.1", latest).check()
        self.assertEqual((r["state"], r["latest"]), ("update", "1.2.0"))
        self.assertTrue(r["url"].endswith("/v1.2.0"))
        self.assertEqual(self.checker("release", "1.2.0", latest).check()["state"], "current")
        self.assertEqual(self.checker("release", "1.10.0", latest).check()["state"], "current")  # not text order
        c = self.checker("release", "1.1.1", latest)
        c.check()
        self.assertFalse(any("ghcr.io" in u or "compare" in u for u in c._http.calls))  # never looks at develop

    def test_develop_compares_with_the_newest_develop_image(self):
        r = self.checker("develop", "1.2.0-dev.27", develop_routes(THEIRS)).check()
        self.assertEqual((r["state"], r["latest"], r["behind_by"]), ("behind", "1.2.0-dev.30", 3))
        self.assertIn("/compare/", r["url"])
        same = self.checker("develop", "1.2.0-dev.30", develop_routes(MINE))
        self.assertEqual(same.check()["state"], "current")
        self.assertFalse(any("/compare/" in u for u in same._http.calls))
        c = self.checker("develop", "1.2.0-dev.27", develop_routes(THEIRS))
        c.check()
        self.assertFalse(any("releases/latest" in u for u in c._http.calls))  # never looks at releases

    def test_failures_and_local_builds(self):
        r = self.checker("develop", "1.2.0-dev.27", {"https://ghcr.io/token": 503}).check()
        self.assertEqual((r["state"], r["detail"]), ("error", "Couldn't check GitHub: HTTP 503"))
        self.assertFalse(UpdateChecker(Build("1.1.1", "local", "", "")).checkable)
        self.assertTrue(self.checker("develop", "x", {}).checkable)


class StatusTests(unittest.TestCase):
    def test_status_shows_the_build_and_the_check_while_it_is_on(self):
        h = AppHarness()
        c = h.setup_admin()
        try:
            s = c.get("/api/v1/status").json()
            self.assertEqual((s["build"]["channel"], s["update"]), ("local", None))
            self.assertEqual(c.get("/api/health").json()["version"], s["build"]["version"])
            h.app.state.updates.result = {"state": "behind", "checked_at": "2026-10-25T00:00:00+00:00",
                                          "latest": "1.2.0-dev.30", "behind_by": 3, "url": None, "detail": None}
            self.assertEqual(c.get("/api/v1/status").json()["update"]["behind_by"], 3)
            c.patch("/api/v1/settings", json={"update_check": False})
            self.assertIsNone(c.get("/api/v1/status").json()["update"])
        finally:
            h.close()
