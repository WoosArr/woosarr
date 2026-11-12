"""Is this the newest build? Checked against GitHub at startup and then every update_check_minutes.

Each build compares only with its own channel:
- release builds: GitHub's latest release ("1.3.0 is available").
- develop builds: the commit of the newest successful *Docker publish* run on develop (that run
  pushed the :develop image), then GitHub counts how many commits that is ahead of ours ("2
  commits behind"). The newest *image*, not the branch head: docs-only commits don't produce one.
  The image registry itself isn't read: GHCR counts every manifest read as a download, so a check
  every few minutes showed up as hundreds of downloads of each develop build.
- local and other builds aren't checked.

All anonymous: one request to GitHub's API per check, two when a newer build exists (GitHub allows
60 an hour per address; the shortest interval, 5 min, uses at most 24). Settings -> General:
on/off and how often (6 h by default, down to 5 min).
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from datetime import UTC, datetime
from typing import Any

import requests

from woosarr.build import Build

log = logging.getLogger("woosarr.updates")

REPO = "WoosArr/woosarr"
WORKFLOW = "docker-publish.yml"  # publishes :develop when a run on develop succeeds
FIRST_CHECK_SECONDS = 60
TICK_SECONDS = 60  # how often the interval setting is looked at
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
        theirs = self._newest_develop_commit()
        if not theirs or theirs == self.build.commit:
            log.info("update check: this is the newest develop build (%s)", self.build.version)
            return {"state": "current", "latest": self.build.version}
        r = self._http.get(f"https://api.github.com/repos/{REPO}/compare/{self.build.commit}...{theirs}",
                           timeout=TIMEOUT)
        r.raise_for_status()
        ahead = int(r.json().get("ahead_by", 0))  # commits the newest build has that this one doesn't
        if ahead <= 0:
            return {"state": "current", "latest": self.build.version}
        latest = _dev_version_after(self.build.version, ahead)
        log.info("update check: newest develop build is %s, %d commit%s newer than this one (%s)",
                 latest or theirs[:7], ahead, "" if ahead == 1 else "s", self.build.version)
        return {"state": "behind", "latest": latest, "behind_by": ahead,
                "url": f"https://github.com/{REPO}/compare/{self.build.commit[:12]}...{theirs[:12]}"}

    def _newest_develop_commit(self) -> str:
        """The commit the newest :develop image was built from: its publish run's commit."""
        runs = self._get_json(f"https://api.github.com/repos/{REPO}/actions/workflows/{WORKFLOW}/runs"
                              "?branch=develop&status=success&per_page=1",
                              {"Accept": "application/vnd.github+json"})["workflow_runs"]
        return str(runs[0]["head_sha"]) if runs else ""

    def _get_json(self, url: str, headers: dict[str, str] | None = None) -> Any:
        r = self._http.get(url, headers=headers, timeout=TIMEOUT)
        r.raise_for_status()
        return r.json()


def _dev_version_after(mine: str, ahead: int) -> str | None:
    """Develop versions count commits since the last release (1.2.0-dev.53), so the build `ahead`
    commits newer is 1.2.0-dev.(53 + ahead). None if this version isn't one of those."""
    m = re.fullmatch(r"(.+-dev\.)(\d+)", mine)
    return f"{m.group(1)}{int(m.group(2)) + ahead}" if m else None


def _describe(exc: Exception) -> str:
    if isinstance(exc, requests.HTTPError) and exc.response is not None:
        return f"HTTP {exc.response.status_code}"
    if isinstance(exc, requests.Timeout):
        return "timed out"
    if isinstance(exc, requests.ConnectionError):
        return "couldn't connect"
    return type(exc).__name__


async def run_checker(checker: UpdateChecker, settings: Any) -> None:
    """At startup (after a minute), then whenever the chosen interval has passed. settings() returns
    (on, minutes); it's read every TICK_SECONDS, so a shorter interval applies straight away."""
    await asyncio.sleep(FIRST_CHECK_SECONDS)
    last: float | None = None
    while True:
        try:
            on, minutes = await asyncio.to_thread(settings)
            if not on:
                checker.result, last = None, None
            elif last is None or time.monotonic() - last >= minutes * 60:
                await asyncio.to_thread(checker.check)
                last = time.monotonic()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(TICK_SECONDS)
