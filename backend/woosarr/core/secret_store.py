"""Encrypted secrets in the database (plan D5, D6, N7).

Values are Fernet-encrypted with the master key and registered with log
redaction as soon as they are written or read. The API never returns them;
use is_set() / secrets.mask() for display.
"""

from __future__ import annotations

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from woosarr.core import redaction
from woosarr.core.secrets import SecretBox, SecretError
from woosarr.db.models import Secret

log = logging.getLogger("woosarr.secrets")

# Known names, so typos don't silently create new secrets.
PLEX_TOKEN = "plex.token"
DISCORD_BOT_TOKEN = "discord.bot_token"
KNOWN = frozenset({PLEX_TOKEN, DISCORD_BOT_TOKEN})
OIDC_SECRET_PREFIX = "oidc.client_secret."


def oidc_client_secret(provider_id: int) -> str:
    return f"{OIDC_SECRET_PREFIX}{provider_id}"


class SecretStore:
    def __init__(self, box: SecretBox) -> None:
        self._box = box

    @staticmethod
    def _check(name: str) -> None:
        if name not in KNOWN and not (name.startswith(OIDC_SECRET_PREFIX) and name[len(OIDC_SECRET_PREFIX):].isdigit()):
            raise KeyError(f"unknown secret {name!r}")

    def set(self, session: Session, name: str, value: str) -> None:
        self._check(name)
        redaction.register_secret(value)
        row = session.get(Secret, name)
        ciphertext = self._box.encrypt(value)
        if row is None:
            session.add(Secret(name=name, ciphertext=ciphertext))
        else:
            row.ciphertext = ciphertext

    def get(self, session: Session, name: str) -> str | None:
        self._check(name)
        row = session.get(Secret, name)
        if row is None:
            return None
        value = self._box.decrypt(row.ciphertext)
        redaction.register_secret(value)
        return value

    def delete(self, session: Session, name: str) -> None:
        self._check(name)
        row = session.get(Secret, name)
        if row is not None:
            session.delete(row)

    def is_set(self, session: Session, name: str) -> bool:
        self._check(name)
        return session.get(Secret, name) is not None

    def register_all(self, session: Session) -> list[str]:
        """At startup: make every stored value known to the log redactor.

        Returns the names that can't be decrypted (wrong or lost master key).
        Those are skipped rather than stopping startup, so the admin can sign
        in and enter the tokens again.
        """
        unreadable = []
        for row in session.scalars(select(Secret)):
            try:
                redaction.register_secret(self._box.decrypt(row.ciphertext))
            except SecretError:
                unreadable.append(row.name)
                log.error("stored secret %s can't be decrypted with the current master key; enter it again", row.name)
        return unreadable

    def readable(self, session: Session, name: str) -> bool:
        try:
            return self.get(session, name) is not None
        except SecretError:
            return False

    def rotate(self, session: Session) -> int:
        """Re-encrypt every secret with the current key (after a key change)."""
        rows = list(session.scalars(select(Secret)))
        for row in rows:
            row.ciphertext = self._box.reencrypt(row.ciphertext)
        return len(rows)
