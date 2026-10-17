import unittest
from pathlib import Path

from woosarr.openapi import schema_json

SNAPSHOT = Path(__file__).resolve().parents[2] / "frontend" / "openapi.json"


class OpenApiSnapshotTests(unittest.TestCase):
    def test_frontend_snapshot_is_current(self):
        # The UI's TypeScript types are generated from this file. If this fails,
        # run `make openapi` and fix any frontend type errors it causes.
        self.assertTrue(SNAPSHOT.exists(), "frontend/openapi.json missing; run `make openapi`")
        self.assertEqual(SNAPSHOT.read_text(), schema_json(), "frontend/openapi.json is stale; run `make openapi`")
