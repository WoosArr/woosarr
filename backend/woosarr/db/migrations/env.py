"""Alembic environment. The engine is passed in by woosarr.db.session.migrate."""

from alembic import context

from woosarr.db.models import Base

config = context.config
engine = config.attributes["engine"]

with engine.begin() as connection:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        render_as_batch=True,  # SQLite needs batch mode for ALTER TABLE
        compare_type=True,
    )
    context.run_migrations()
