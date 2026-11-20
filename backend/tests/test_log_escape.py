"""Values in WooSarr's log lines can't start a fake new line or carry terminal escapes (CodeQL py/log-injection)."""

import logging
import unittest

from tests.helpers import AppHarness
from woosarr.core import logs
from woosarr.core.redaction import Multiline, escape_controls

log = logging.getLogger("woosarr.test")


class EscapeControlsTests(unittest.TestCase):
    def test_line_breaks_and_escapes_are_shown_not_obeyed(self):
        self.assertEqual(escape_controls("a\nb\r\x1b[31mc\x00 "), "a\\nb\\r\\x1b[31mc\\x00\\u2028")

    def test_tab_unicode_and_emoji_stay(self):
        self.assertEqual(escape_controls("tab\there ünï 🎬"), "tab\there ünï 🎬")


class LoggedValuesTests(unittest.TestCase):
    def setUp(self):
        self.h = AppHarness()
        self.c = self.h.setup_admin()

    def tearDown(self):
        self.h.close()

    def message(self, marker: str) -> str:
        logs.flush()
        entries = self.c.get("/api/v1/logs", params={"q": marker}).json()["entries"]
        self.assertEqual(len(entries), 1, entries)
        return entries[0]["message"]

    def test_a_typed_username_cannot_forge_a_second_line(self):
        log.warning("m1 login failed username=%s ip=%s", "bob\n2026-11-20 ERROR fake entry", "10.0.0.1")
        self.assertEqual(self.message("m1 login"),
                         "m1 login failed username=bob\\n2026-11-20 ERROR fake entry ip=10.0.0.1")

    def test_numbers_repr_exceptions_and_dict_args_still_format(self):
        log.warning("m2 %d item(s) in %.1f s, title %r, error %s", 3, 1.25, "a\nb", ValueError("bad\nvalue"))
        self.assertEqual(self.message("m2 "), "m2 3 item(s) in 1.2 s, title 'a\\nb', error bad\\nvalue")
        log.warning("m3 %(who)s", {"who": "x\ny"})
        self.assertEqual(self.message("m3 "), "m3 x\\ny")

    def test_the_message_text_and_multiline_values_keep_their_line_breaks(self):
        log.warning("m4 running:\n%s", Multiline("  File a\n  File b"))
        self.assertEqual(self.message("m4 running"), "m4 running:\n  File a\n  File b")

    def test_library_records_are_left_alone(self):
        logging.getLogger("discord.gateway").warning("m5 traceback:\n%s", "line 1\nline 2")
        self.assertEqual(self.message("m5 traceback"), "m5 traceback:\nline 1\nline 2")

    def test_secrets_are_still_redacted(self):
        token = "wsk_" + "abcd1234" + "_" + "x" * 40
        log.warning("m6 key %s", token + "\n")
        message = self.message("m6 key")
        self.assertNotIn("x" * 40, message)
        self.assertTrue(message.endswith("\\n"))


if __name__ == "__main__":
    unittest.main()
