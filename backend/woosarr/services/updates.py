"""Is this the newest build? Checked against GitHub at startup and every CHECK_HOURS.

Each build compares only with its own channel:
- release builds: GitHub's latest release ("1.3.0 is available").
- develop builds: the commit the newest :develop image was built from (its label in the image
  registry), then GitHub counts how many commits that is ahead of ours ("2 commits behind").
  The newest *image*, not the branch head: docs-only commits don't produce one.
- local and other builds aren't checked.

All anonymous, a handful of small requests per check (GitHub allows 60 an hour per address).
Off with Settings -> General -> "Check GitHub for newer versions".
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime
from typing import Any

import requests

from woosarr.build import Build

log = logging.getLogger("woosarr.updates")

REPO = "WoosArr/woosarr"
IMAGE = "woosarr/woosarr"
FIRST_CHECK_SECONDS = 60
CHECK_HOURS = 6
TIMEOUT = 10


def _version_tuple(v: str) -> tuple[int, ...] | None:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", v.strip())
    return tuple(int(x) for x in m.groups()) if m else None


class UpdateChecker:
    def __init__(self, build: Build, session: requests.Session | None = None) -> None:
        self.build = build
        self._http = session or requests.Session()
        self._http.headers["User-Agent"] = f"WooSarr/{build.version} (+https://github.com/{REPO})"
        self.result: dict[str, Any] | None = None

    @property
    def checkable(self) -> bool:
        return self.build.channel in ("release", "develop") and bool(self.build.commit)

    def check(self) -> dict[str, Any]:
        now = datetime.now(UTC).isoformat()
        try:
            result = self._release() if self.build.channel == "release" else self._develop()
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError) as exc:
            log.info("update check failed: %s", _describe(exc))
            result = {"state": "error", "detail": f"Couldn't check GitHub: {_describe(exc)}"}
        self.result = {"checked_at": now, "latest": None, "behind_by": None, "url": None, "detail": None, **result}
        return self.result

    # ---- release builds ---------------------------------------------------------------------

    def _release(self) -> dict[str, Any]:
        r = self._http.get(f"https://api.github.com/repos/{REPO}/releases/latest", timeout=TIMEOUT)
        r.raise_for_status()
        data = r.json()
        latest, mine = str(data["tag_name"]).lstrip("v"), self.build.version
        newer = (_version_tuple(latest) or ()) > (_version_tuple(mine) or ())
        log.info("update check: newest release %s, this is %s%s", latest, mine, " (update available)" if newer else "")
        return {"state": "update" if newer else "current", "latest": latest, "url": data.get("html_url")}

    # ---- develop builds ---------------------------------------------------------------------

    def _develop(self) -> dict[str, Any]:
        labels = self._newest_develop_labels()
        theirs = labels.get("org.opencontainers.image.revision", "")
        latest = labels.get("io.woosarr.version") or None
        if not theirs or theirs == self.build.commit:
            log.info("update check: this is the newest develop build (%s)", self.build.version)
            return {"state": "current", "latest": latest or self.build.version}
        r = self._http.get(f"https://api.github.com/repos/{REPO}/compare/{self.build.commit}...{theirs}",
                           timeout=TIMEOUT)
        r.raise_for_status()
        ahead = int(r.json().get("ahead_by", 0))  # commits the newest build has that this one doesn't
        if ahead <= 0:
            return {"state": "current", "latest": latest or self.build.version}
        log.info("update check: newest develop build is %s, %d commit%s newer than this one (%s)",
                 latest or theirs[:7], ahead, "" if ahead == 1 else "s", self.build.version)
        return {"state": "behind", "latest": latest, "behind_by": ahead,
                "url": f"https://github.com/{REPO}/compare/{self.build.commit[:12]}...{theirs[:12]}"}

    def _newest_develop_labels(self) -> dict[str, str]:
        """Labels of the newest :develop image (anonymous pull token; GHCR is public)."""
        token = self._get_json(f"https://ghcr.io/token?scope=repository:{IMAGE}:pull")["token"]
        auth = {"Authorization": f"Bearer {token}"}
        index = self._get_json(f"https://ghcr.io/v2/{IMAGE}/manifests/develop", {
            **auth, "Accept": "application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json"})
        manifest = index
        if "manifests" in index:  # multi-arch: any platform has the same labels
            digest = next(m["digest"] for m in index["manifests"]
                          if m.get("platform", {}).get("architecture") not in (None, "unknown"))
            manifest = self._get_json(f"https://ghcr.io/v2/{IMAGE}/manifests/{digest}",
                                      {**auth, "Accept": "application/vnd.oci.image.manifest.v1+json"})
        config = self._get_json(f"https://ghcr.io/v2/{IMAGE}/blobs/{manifest['config']['digest']}", auth)
        return dict((config.get("config") or {}).get("Labels") or {})

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        r = self._http.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()


def _describe(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't connect"
    return type(exc).__name__


async def run_checker(checker: UpdateChecker, enabled: Any) -> None:
    """At startup (after a minute) and every CHECK_HOURS, while the setting is on."""
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    while True:
        try:
            if await asyncio.to_thread(enabled):
                await asyncio.to_thread(checker.check)
            else:
                checker.result = None
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(CHECK_HOURS * 3600)
