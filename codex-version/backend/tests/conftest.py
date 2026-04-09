from __future__ import annotations

import os
from collections.abc import Generator

import pytest


@pytest.fixture(scope="session")
def postgres_url() -> Generator[str, None, None]:
    existing = os.getenv("DATABASE_URL")
    if existing:
        yield existing
        return

    from testcontainers.postgres import PostgresContainer

    with PostgresContainer("postgres:16-alpine") as pg:
        yield pg.get_connection_url().replace("psycopg2", "asyncpg")


@pytest.fixture(scope="session")
def redis_url() -> Generator[str, None, None]:
    existing = os.getenv("REDIS_URL")
    if existing:
        yield existing
        return

    from testcontainers.redis import RedisContainer

    with RedisContainer("redis:7-alpine") as redis:
        host = redis.get_container_host_ip()
        port = redis.get_exposed_port(redis.port)
        yield f"redis://{host}:{port}/0"
