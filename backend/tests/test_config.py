import os
import unittest
from pathlib import Path
from unittest import mock

from woosarr.core.config import DEFAULT_PORT, Settings


class ConfigTests(unittest.TestCase):
    def test_defaults_need_only_a_config_volume(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            s = Settings()
        self.assertEqual(s.port, 31337)
        self.assertEqual(DEFAULT_PORT, 31337)
        self.assertEqual(s.config_dir, Path("/config"))
        self.assertEqual(s.log_file, Path("/config/logs/woosarr.log"))

    def test_env_overrides(self):
        env = {"WOOSARR_PORT": "8080", "WOOSARR_CONFIG_DIR": "/data", "WOOSARR_LOG_LEVEL": "DEBUG"}
        with mock.patch.dict(os.environ, env, clear=True):
            s = Settings()
        self.assertEqual(s.port, 8080)
        self.assertEqual(s.config_dir, Path("/data"))
        self.assertEqual(s.log_level, "DEBUG")

    def test_no_secret_shaped_settings(self):
        # Secrets belong in the encrypted store, never in process config defaults (N1).
        for name in Settings.model_fields:
            for word in ("token", "secret", "password", "key"):
                self.assertNotIn(word, name.lower(), name)
