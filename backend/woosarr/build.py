"""What this build is: version, channel (release / develop / dev / local), commit and commit date.

CI bakes them into the image (scripts/build-info.sh -> WOOSARR_BUILD_* in docker/Dockerfile). A
release reads like 1.2.0; a develop build like 1.3.0-dev.14, where 14 counts the commits since the
last release, so a higher number is always newer. Anything else (make dev, tests) is a local build
of __version__.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime

from woosarr import __version__


@dataclass(frozen=True)
class Build:
    version: str
    channel: str  # release | develop | dev | local
    commit: str  # full SHA, "" for local builds
    date: str  # ISO commit date, "" for local builds

    @property
    def short_commit(self) -> str:
        return self.commit[:7]

    def label(self) -> str:
        """"1.3.0-dev.14 (develop, ba15809, 25 Oct 2026)" / "1.2.0 (release)" / "1.1.1 (local build)"."""
        if self.channel == "local":
            return f"{self.version} (local build)"
        bits = [self.channel] + ([self.short_commit] if self.short_commit else [])
        try:
            bits.append(datetime.fromisoformat(self.date).strftime("%-d %b %Y"))
        except ValueError:
            pass
        return f"{self.version} ({', '.join(bits)})"


def current() -> Build:
    env = os.environ
    return Build(version=env.get("WOOSARR_BUILD_VERSION") or __version__,
                 channel=env.get("WOOSARR_BUILD_CHANNEL") or "local",
                 commit=env.get("WOOSARR_BUILD_COMMIT", ""), date=env.get("WOOSARR_BUILD_DATE", ""))
