import os
import stat
import tempfile
import unittest
from pathlib import Path

from cryptography.fernet import Fernet

from woosarr.core.secrets import MASK, SecretBox, SecretError, load_or_create_key, mask


class SecretsTests(unittest.TestCase):
    def test_roundtrip_and_ciphertext_hides_plaintext(self):
        box = SecretBox(Fernet.generate_key())
        ct = box.encrypt("super-secret-token")
        self.assertNotIn("super-secret-token", ct)
        self.assertEqual(box.decrypt(ct), "super-secret-token")

    def test_wrong_key_fails(self):
        ct = SecretBox(Fernet.generate_key()).encrypt("x")
        with self.assertRaises(SecretError):
            SecretBox(Fernet.generate_key()).decrypt(ct)

    def test_rotation_reencrypts_with_new_key(self):
        old, new = Fernet.generate_key(), Fernet.generate_key()
        ct_old = SecretBox(old).encrypt("value")
        rotated = SecretBox(new, previous_keys=(old,))
        ct_new = rotated.reencrypt(ct_old)
        self.assertEqual(SecretBox(new).decrypt(ct_new), "value")

    def test_key_file_created_with_0600(self):
        with tempfile.TemporaryDirectory() as tmp:
            key = load_or_create_key(Path(tmp), env={})
            path = Path(tmp) / "secrets" / "master.key"
            self.assertTrue(path.exists())
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(load_or_create_key(Path(tmp), env={}), key)

    def test_loose_permissions_are_tightened(self):
        with tempfile.TemporaryDirectory() as tmp:
            load_or_create_key(Path(tmp), env={})
            path = Path(tmp) / "secrets" / "master.key"
            os.chmod(path, 0o644)
            load_or_create_key(Path(tmp), env={})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_env_key_takes_precedence_and_writes_no_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            env_key = Fernet.generate_key().decode()
            key = load_or_create_key(Path(tmp), env={"WOOSARR_MASTER_KEY": env_key})
            self.assertEqual(key, env_key.encode())
            self.assertFalse((Path(tmp) / "secrets").exists())

    def test_mask_never_reveals_value(self):
        self.assertEqual(mask("abc"), MASK)
        self.assertEqual(mask(None), "")
        self.assertNotIn("abc", mask("abc"))


if __name__ == "__main__":
    unittest.main()
