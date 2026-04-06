"""Alembic environment configuration for async PostgreSQL migrations.

This module wires Alembic to the MSAI v2 async SQLAlchemy engine so that
``alembic revision --autogenerate`` and ``alembic upgrade head`` work with
the asyncpg driver.

The database URL is read from :pydata:`msai.core.config.settings` at runtime,
overriding any value in ``alembic.ini``.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from msai.core.config import settings
from msai.models import Base  # Imports all models so Base.metadata has full schema

# --------------------------------------------------------------------------- #
# Alembic Config object -- provides access to values in alembic.ini.
# --------------------------------------------------------------------------- #
config = context.config

# Interpret the config file for Python logging (unless we're in a test).
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Override the sqlalchemy.url from alembic.ini with the application setting.
config.set_main_option("sqlalchemy.url", settings.database_url)

# The metadata that Alembic inspects for autogenerate support.
target_metadata = Base.metadata


# --------------------------------------------------------------------------- #
# Offline (SQL-script) migrations
# --------------------------------------------------------------------------- #


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode.

    Configures the context with just a URL (no engine needed) and emits
    ``BEGIN``/``COMMIT`` around each migration script.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# --------------------------------------------------------------------------- #
# Online (async engine) migrations
# --------------------------------------------------------------------------- #


def do_run_migrations(connection: Connection) -> None:
    """Configure the Alembic context with a live connection and run migrations."""
    context.configure(connection=connection, target_metadata=target_metadata)

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Create an async engine and run migrations inside its connection."""
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode using an async engine."""
    asyncio.run(run_async_migrations())


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
