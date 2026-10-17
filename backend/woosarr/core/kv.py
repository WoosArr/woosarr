"""Small internal key/value state in the `settings` table (not admin settings)."""

from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from woosarr.db.models import Setting


def get(session: Session, key: str, default: Any = None) -> Any:
    row = session.get(Setting, key)
    return row.value if row is not None else default


def put(session: Session, key: str, value: Any) -> None:
    row = session.get(Setting, key)
    if row is None:
        session.add(Setting(key=key, value=value))
    else:
        row.value = value


def delete(session: Session, key: str) -> None:
    row = session.get(Setting, key)
    if row is not None:
        session.delete(row)
