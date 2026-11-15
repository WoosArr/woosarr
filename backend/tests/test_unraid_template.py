"""The Unraid template's <Date> and <Changes> follow the newest release (scripts/unraid_template.py).

Community Applications shows WooSarr as updated only through those two tags (it can't read GHCR push
dates), so a release whose template lags would look unchanged in the Apps tab (live 2026-11-14).
"""

import importlib.util
import unittest
import xml.dom.minidom
from pathlib import Path

from woosarr import __version__

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("unraid_template", ROOT / "scripts" / "unraid_template.py")
unraid_template = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(unraid_template)


class UnraidTemplateTests(unittest.TestCase):
    def test_template_matches_the_newest_release(self):
        self.assertEqual(unraid_template.main(["--check"]), 0,
                         "run: python3 scripts/unraid_template.py (in the release commit)")

    def test_newest_release_is_this_version(self):
        version, _date, _summary = unraid_template.latest_release(
            (ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
        self.assertEqual(version, __version__, "the release commit bumps the version and the CHANGELOG together")

    def test_template_is_valid_xml_with_date_and_changes(self):
        doc = xml.dom.minidom.parse(str(ROOT / "unraid" / "woosarr.xml"))
        date = doc.getElementsByTagName("Date")[0].firstChild.data
        changes = doc.getElementsByTagName("Changes")[0].firstChild.data
        self.assertRegex(date, r"^\d{4}-\d{2}-\d{2}$")
        self.assertTrue(changes.startswith(f"### {__version__} ({date})"))

    def test_a_new_release_rewrites_date_and_changes(self):
        changelog = ("# Changelog\n\n## [Unreleased]\n\n### Fixed\n- pending\n\n"
                     "## [9.1.0] - 2031-02-03\n\nBig & better.\n\n### Added\n- x\n\n## [9.0.0] - 2031-01-01\n\nOld.\n")
        self.assertEqual(unraid_template.latest_release(changelog), ("9.1.0", "2031-02-03", "Big & better."))
        template = (ROOT / "unraid" / "woosarr.xml").read_text(encoding="utf-8")
        out = unraid_template.render(template, "9.1.0", "2031-02-03", "Big & better.")
        self.assertEqual(out.count("<Date>"), 1)
        self.assertIn("<Date>2031-02-03</Date>", out)
        self.assertIn("### 9.1.0 (2031-02-03)\nBig &amp; better.", out)
        xml.dom.minidom.parseString(out)  # still valid
