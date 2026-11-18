"""Engine, sessions and startup migrations for the SQLite database."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, event
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session, sessionmaker

from woosarr.db import busy

log = logging.getLogger("woosarr.db")

MIGRATIONS_DIR = Path(__file__).parent / "migrations"
WROTE = "woosarr.wrote"  # Session.info key
BUSY_TIMEOUT_MS = 30000  # how long a write waits for SQLite's lock before failing


def make_engine(db_path: Path | str) -> Engine:
    url = "sqlite://" if str(db_path) == ":memory:" else f"sqlite:///{db_path}"
    engine = create_engine(url, connect_args={"check_same_thread": False, "timeout": 30})

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _record) -> None:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
        cur.close()

    return engine


TRY_WAIT_MS = 250


def try_write(session: Session, stmt, what: str) -> bool:
    """Run a write that isn't worth failing or holding up a request for; False if it was skipped.

    While another write holds SQLite's lock (live 2026-11-18, a stalled disk), waiting the usual 30 s
    made every page queue up behind a "last seen" time or an alert resolution, until the connection
    pool ran out and pages failed. Reads still work then, so the request goes on without the write and a
    later request makes it instead.
    """
    wrote = session.info.get(WROTE)
    conn = session.connection()
    conn.exec_driver_sql(f"PRAGMA busy_timeout={TRY_WAIT_MS}")
    try:
        session.execute(stmt)
    except OperationalError as exc:
        if busy.reason(exc) is None:
            raise
        session.info[WROTE] = wrote  # nothing was written after all
        log.info("%s not saved: the database is locked by another write (skipped; a later request saves it)", what)
        return False
    finally:
        conn.exec_driver_sql(f"PRAGMA busy_timeout={BUSY_TIMEOUT_MS}")
    return True


def alembic_config(engine: Engine) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    cfg.attributes["engine"] = engine
    return cfg


def migrate(engine: Engine) -> None:
    """Bring the schema to the latest revision (safe to run on every start)."""
    command.upgrade(alembic_config(engine), "head")
    log.info("database schema up to date")


class Database:
    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._factory = sessionmaker(engine, expire_on_commit=False)

        # Remember whether a unit of work wrote anything, so a committed write can clear the
        # "database busy" warning (db/busy.py). A read-only commit proves nothing about the lock.
        @event.listens_for(self._factory, "after_flush")
        def _flushed(session: Session, _ctx) -> None:
            session.info[WROTE] = True

        @event.listens_for(self._factory, "do_orm_execute")
        def _executed(state) -> None:
            if state.is_insert or state.is_update or state.is_delete:
                state.session.info[WROTE] = True

    @contextmanager
    def session(self) -> Iterator[Session]:
        """A unit of work: commits on success, rolls back on error."""
        s = self._factory()
        try:
            yield s
            s.commit()
            if s.info.pop(WROTE, False):
                busy.write_committed()
        except BaseException:
            s.rollback()
            raise
        finally:
            s.close()
