#!/usr/bin/env python3
"""Keep the Unraid template's <Date> and <Changes> in step with the newest release in CHANGELOG.md.

Community Applications can't read when a GHCR image was pushed (its registry scan covers Docker Hub),
so for WooSarr the listing's "updated" date and changelog come from the template on `main`:
<Date> (the release date) and <Changes> (what's new). Without them the listing only knows when it was
first seen (live 2026-11-14: the 1.2.0 release didn't show as an update).

    python3 scripts/unraid_template.py           # rewrite unraid/woosarr.xml from CHANGELOG.md
    python3 scripts/unraid_template.py --check   # exit 1 if it's out of date (CI: tests/test_unraid_template.py)

Run it in the release commit (version bump + changelog); CI fails a release whose template lags.
Standard library only.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path
from xml.sax.saxutils import escape

ROOT = Path(__file__).resolve().parent.parent
CHANGELOG = ROOT / "CHANGELOG.md"
TEMPLATE = ROOT / "unraid" / "woosarr.xml"
CHANGELOG_URL = "https://github.com/WoosArr/woosarr/blob/main/CHANGELOG.md"
RELEASE = re.compile(r"^## \[(\d+\.\d+\.\d+)\] - (\d{4}-\d{2}-\d{2})\s*$", re.MULTILINE)


def latest_release(text: str) -> tuple[str, str, str]:
    """(version, date, summary) of the newest released section; the summary is its first paragraph
    that isn't a heading (every release opens with a one-paragraph summary)."""
    m = RELEASE.search(text)
    if m is None:
        raise SystemExit("CHANGELOG.md has no released version (## [X.Y.Z] - YYYY-MM-DD)")
    body = text[m.end():]
    nxt = re.search(r"^## \[", body, re.MULTILINE)
    body = body[:nxt.start()] if nxt else body
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", body) if p.strip()]
    summary = next((p for p in paragraphs if not p.startswith("#")), "")
    return m.group(1), m.group(2), " ".join(summary.split())


def changes(version: str, date: str, summary: str) -> str:
    return f"### {version} ({date})\n{summary}\n\nFull changelog: {CHANGELOG_URL}"


def render(template: str, version: str, date: str, summary: str) -> str:
    block = f"  <Changes>{escape(changes(version, date, summary))}</Changes>\n  <Date>{date}</Date>\n"
    template = re.sub(r"  <Changes>.*?</Changes>\n", "", template, flags=re.DOTALL)
    template = re.sub(r"  <Date>.*?</Date>\n", "", template)
    # Right after <Overview>, so the template reads summary, then what's new.
    return re.sub(r"(  <Overview>.*?</Overview>\n)", lambda m: m.group(1) + block, template, count=1, flags=re.DOTALL)


def main(argv: list[str]) -> int:
    version, date, summary = latest_release(CHANGELOG.read_text(encoding="utf-8"))
    current = TEMPLATE.read_text(encoding="utf-8")
    wanted = render(current, version, date, summary)
    if "--check" in argv:
        if wanted != current:
            print(f"unraid/woosarr.xml is not up to date with CHANGELOG.md ({version}, {date}): "
                  "run python3 scripts/unraid_template.py", file=sys.stderr)
            return 1
        print(f"unraid/woosarr.xml matches {version} ({date})")
        return 0
    if wanted != current:
        TEMPLATE.write_text(wanted, encoding="utf-8")
        print(f"unraid/woosarr.xml updated for {version} ({date})")
    else:
        print(f"unraid/woosarr.xml already matches {version} ({date})")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
