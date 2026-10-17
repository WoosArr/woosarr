"""Encryption for secrets at rest (plan D5, N1, N7).

The master key is never baked into the image or repo. Resolution order:
  1. WOOSARR_MASTER_KEY env var (or a file path in WOOSARR_MASTER_KEY_FILE,
     e.g. a Docker secret)
  2. <config>/secrets/master.key, generated on first run with mode 0600
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

MASK = "********"


class SecretError(Exception):
    pass


def _write_key_file(path: Path, key: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    # O_EXCL so two first-run processes can't both create different keys.
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as fh:
        fh.write(key)


def load_or_create_key(config_dir: Path, env: dict[str, str] | None = None) -> bytes:
    env = os.environ if env is None else env
    explicit = env.get("WOOSARR_MASTER_KEY")
    if explicit:
        return explicit.strip().encode()
    key_file_env = env.get("WOOSARR_MASTER_KEY_FILE")
    if key_file_env:
        return Path(key_file_env).read_bytes().strip()

    path = config_dir / "secrets" / "master.key"
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & 0o077:
            try:
                path.chmod(0o600)
            except OSError as exc:
                raise SecretError(f"master key {path} is group/world accessible") from exc
        return path.read_bytes().strip()
    key = Fernet.generate_key()
    try:
        _write_key_file(path, key)
    except FileExistsError:  # lost the race; use the winner's key
        return path.read_bytes().strip()
    return key


class SecretBox:
    """Encrypt/decrypt short secrets. Supports key rotation."""

    def __init__(self, key: bytes, previous_keys: tuple[bytes, ...] = ()):
        try:
            self._fernet = Fernet(key)
            self._legacy = [Fernet(k) for k in previous_keys]
        except (ValueError, TypeError) as exc:
            raise SecretError("invalid master key format") from exc

    def encrypt(self, plaintext: str) -> str:
        return self._fernet.encrypt(plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        for fernet in (self._fernet, *self._legacy):
            try:
                return fernet.decrypt(ciphertext.encode()).decode()
            except InvalidToken:
                continue
        raise SecretError("could not decrypt secret (wrong or rotated master key?)")

    def reencrypt(self, ciphertext: str) -> str:
        """Decrypt with any known key and re-encrypt with the current one."""
        return self.encrypt(self.decrypt(ciphertext))


def mask(value: str | None) -> str:
    """What the API/UI may show for a stored secret: only whether it is set."""
    return MASK if value else ""
