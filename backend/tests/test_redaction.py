import io
import logging
import unittest

from woosarr.core import redaction

# Canary values: obviously fake, only used to prove they get scrubbed.
PLEX_CANARY = "CANARYplextoken1234567890"
DISCORD_CANARY = "MTIzNDU2Nzg5MDEyMzQ1" + "Njc4OQ" + "." + "GabcDE" + "." + "abcdefghijklmnopqrstuvwxyz0"
PASSWORD_CANARY = "hunter2-" + "canary-pass"  # split: no secret-shaped literal (gitleaks)


def make_logger():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    redaction.install(handler)
    logger = logging.getLogger(f"test.{id(stream)}")
    logger.handlers = [handler]
    logger.propagate = False
    logger.setLevel(logging.DEBUG)
    return logger, stream


class RedactionTests(unittest.TestCase):
    def setUp(self):
        redaction.registry.clear()

    def test_plex_token_in_url_and_header(self):
        log, out = make_logger()
        log.info("GET https://plex.tv/api?X-Plex-Token=%s", PLEX_CANARY)
        log.info("headers {'X-Plex-Token': '%s'}", PLEX_CANARY)
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_registered_exact_secret(self):
        redaction.register_secret("some-odd-secret-value")
        log, out = make_logger()
        log.info("value is some-odd-secret-value ok")
        self.assertNotIn("some-odd-secret-value", out.getvalue())

    def test_discord_token_shape(self):
        log, out = make_logger()
        log.error("login failed for %s", DISCORD_CANARY)
        self.assertNotIn(DISCORD_CANARY, out.getvalue())

    def test_authorization_header(self):
        log, out = make_logger()
        log.debug("Authorization: Bearer %s", PLEX_CANARY)
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_password_kv(self):
        log, out = make_logger()
        log.info("login payload password=%s user=admin", PASSWORD_CANARY)
        self.assertNotIn(PASSWORD_CANARY, out.getvalue())
        self.assertIn("user=admin", out.getvalue())

    def test_exception_traceback_is_scrubbed(self):
        redaction.register_secret(PLEX_CANARY)
        log, out = make_logger()
        try:
            raise RuntimeError(f"bad token {PLEX_CANARY}")
        except RuntimeError:
            log.exception("request failed")
        self.assertNotIn(PLEX_CANARY, out.getvalue())

    def test_normal_text_untouched(self):
        log, out = make_logger()
        log.info("Granted library Movies to user alice")
        self.assertIn("Granted library Movies to user alice", out.getvalue())


if __name__ == "__main__":
    unittest.main()


class DiscordPayloadRedactionTests(unittest.TestCase):
    """Live finding: discord.py DEBUG logs contained interaction tokens."""

    def test_interaction_token_in_event_dict(self):
        tok = "aW50ZXJh" + "Y3Rpb24" * 6
        out = redaction.redact("{'t': 'INTERACTION_CREATE', 'd': {'version': 1, 'type': 2, 'token': '" + tok + "'}}")
        self.assertNotIn(tok, out)
        self.assertIn("'token': '[REDACTED]'", out)

    def test_interaction_and_webhook_urls(self):
        tok = "aW50ZXJh" + "Y3Rpb24" * 6
        for url in (f"POST https://discord.com/api/v10/interactions/1561048885286412379/{tok}/callback",
                    f"https://discord.com/api/v10/webhooks/1560164945125965834/{tok}/messages/@original"):
            out = redaction.redact(url)
            self.assertNotIn(tok, out, url)
            self.assertIn("[REDACTED]", out)

    def test_json_token_fields(self):
        tok = "zz" + "Tok9" * 6
        self.assertNotIn(tok, redaction.redact('{"authToken": "' + tok + '"}'))


class UrlParameterRedactionTests(unittest.TestCase):
    """Live 2026-10-27: the OIDC callback's code and state in uvicorn.access lines, and signed
    GitHub storage links (sig, hmac) in urllib3 DEBUG lines from the update check."""

    def setUp(self):
        redaction.registry.clear()
        # Built at runtime: no secret-shaped literal in source (gitleaks).
        self.code = "1.AVIA" + "Ks3ky8Wb" * 12
        self.state = "dPeQ" + "S0U2wLA2" * 4
        self.sig = "5NWI" + "q8ZrT0x" * 5 + "%3D"
        self.hmac = "913ff18f" + "616ec02e" * 3

    def test_oidc_callback_access_line(self):
        line = (f'10.0.3.131:0 - "GET /api/v1/auth/oidc/callback?code={self.code}&state={self.state}'
                '&session_state=009b5d8a-b85a-3e25-ca0e-7deee5b45abb HTTP/1.1" 303')
        out = redaction.redact(line)
        for secret in (self.code, self.state, "009b5d8a-b85a-3e25-ca0e-7deee5b45abb"):
            self.assertNotIn(secret, out)
        self.assertIn('/api/v1/auth/oidc/callback?code=[REDACTED]&state=[REDACTED]', out)
        self.assertTrue(out.endswith(' HTTP/1.1" 303'))

    def test_signed_download_link(self):
        line = (f'https://pkg-containers.githubusercontent.com:443 "GET /ghcrblobs12/blobs/sha256:0c23?se=2026-10-26'
                f'&sig={self.sig}&sp=r&hmac={self.hmac} HTTP/1.1" 200 None')
        out = redaction.redact(line)
        self.assertNotIn(self.sig, out)
        self.assertNotIn(self.hmac, out)
        self.assertIn("se=2026-10-26", out)  # the rest of the URL stays readable

    def test_other_query_values_untouched(self):
        line = '"GET /api/v1/logs?level=WARNING&limit=200&q=code HTTP/1.1" 200'
        self.assertEqual(redaction.redact(line), line)


class ScrubFilesTests(unittest.TestCase):
    """Lines written before a pattern existed are scrubbed from the log files once."""

    def setUp(self):
        import json
        import tempfile
        from pathlib import Path

        self.json = json
        self.dir = Path(tempfile.mkdtemp())
        self.code = "1.AVIA" + "Ks3ky8Wb" * 12
        callback = {"ts": "t", "level": "INFO", "logger": "uvicorn.access",
                    "message": f'1.2.3.4:0 - "GET /api/v1/auth/oidc/callback?code={self.code}&state=x" 303'}
        plain = {"ts": "t", "level": "INFO", "logger": "woosarr.app", "message": 'said "hi" \\ ok'}
        self.plain_line = json.dumps(plain, ensure_ascii=False)
        (self.dir / "woosarr.log").write_text(json.dumps(callback) + "\n" + self.plain_line + "\n")
        (self.dir / "woosarr.log.1").write_text(json.dumps(callback) + "\nnot json ?code=" + self.code + "\n")
        (self.dir / "alerts.log").write_text(self.plain_line + "\n")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.dir)

    def test_once_per_version_and_json_stays_valid(self):
        self.assertEqual(redaction.scrub_files(self.dir), {"woosarr.log": 1, "woosarr.log.1": 2})
        for name in ("woosarr.log", "woosarr.log.1", "alerts.log"):
            text = (self.dir / name).read_text()
            self.assertNotIn(self.code, text)
        lines = (self.dir / "woosarr.log").read_text().splitlines()
        self.assertIn("code=[REDACTED]", self.json.loads(lines[0])["message"])
        self.assertEqual(lines[1], self.plain_line)  # unchanged lines are kept byte for byte
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()),
                         [".redaction-version", "alerts.log", "woosarr.log", "woosarr.log.1"])
        # Done for this version: a secret in the file now (it couldn't be, but) is left alone.
        (self.dir / "alerts.log").write_text("?code=" + self.code + "\n")
        self.assertEqual(redaction.scrub_files(self.dir), {})
