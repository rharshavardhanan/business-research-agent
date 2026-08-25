import os
import uuid

import psycopg
import pytest

TEST_DSN = os.environ.get(
    "DATABASE_URL_TEST", "postgresql://postgres:test@localhost:55432/bra"
)


@pytest.fixture
def dsn():
    """A throwaway schema per test, so tests cannot see each other's rows."""
    name = f"t_{uuid.uuid4().hex[:12]}"
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f"CREATE SCHEMA {name}")
    yield f"{TEST_DSN}?options=-csearch_path%3D{name}"
    with psycopg.connect(TEST_DSN, autocommit=True) as conn:
        conn.execute(f"DROP SCHEMA {name} CASCADE")


@pytest.fixture
def store(dsn):
    from app.store import Store

    s = Store(dsn)
    s.init_schema()
    return s
