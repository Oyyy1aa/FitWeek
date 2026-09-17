"""Integration fixtures with strict test-resource guards."""

import os
from collections.abc import AsyncIterator, Iterator
from urllib.parse import urlsplit

import pytest
import pytest_asyncio
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from app.config import Settings
from app.persistence.database import Database


class RedactedTestDatabaseUrl(str):
    """Keep subprocess-compatible test URLs out of failure representations."""

    def __repr__(self) -> str:
        return "<redacted test database url>"


@pytest.fixture(scope="session", autouse=True)
def local_processes_bypass_developer_proxy() -> Iterator[None]:
    """Keep loopback-only process tests independent from desktop proxy settings."""

    names = (
        "ALL_PROXY",
        "all_proxy",
        "HTTP_PROXY",
        "http_proxy",
        "HTTPS_PROXY",
        "https_proxy",
        "NO_PROXY",
        "no_proxy",
    )
    original = {name: os.environ.get(name) for name in names}
    for name in names:
        if name.lower() != "no_proxy":
            os.environ.pop(name, None)
    os.environ["NO_PROXY"] = "127.0.0.1,localhost"
    os.environ["no_proxy"] = "127.0.0.1,localhost"
    yield
    for name, value in original.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value


@pytest.fixture
def test_redis_url() -> str:
    """Return an isolated non-development Redis URL without exposing it."""

    raw_url = os.getenv("TEST_REDIS_URL")
    if raw_url is None:
        pytest.fail("TEST_REDIS_URL is required for Redis integration tests")
    parsed_url = urlsplit(raw_url)
    database_path = parsed_url.path.strip("/")
    try:
        database = int(database_path)
    except ValueError:
        pytest.fail("TEST_REDIS_URL must select a numeric Redis database")
    if (
        parsed_url.scheme not in {"redis", "rediss"}
        or not parsed_url.hostname
        or database < 1
        or database > 15
    ):
        pytest.fail("TEST_REDIS_URL must select a non-development Redis database")
    return raw_url


@pytest.fixture
def mysql_test_url() -> str:
    raw_url = os.getenv("TEST_DATABASE_URL")
    if raw_url is None:
        pytest.skip("TEST_DATABASE_URL is required for MySQL integration tests")
    parsed_url = make_url(raw_url)
    if parsed_url.drivername != "mysql+asyncmy":
        pytest.fail("integration database must use mysql+asyncmy")
    if not (parsed_url.database or "").endswith("_test"):
        pytest.fail("integration database name must end with '_test'")
    return RedactedTestDatabaseUrl(raw_url)


@pytest_asyncio.fixture
async def mysql_test_database(mysql_test_url: str) -> AsyncIterator[Database]:
    settings = Settings(
        app_env="test",
        database_url=SecretStr(mysql_test_url),
        redis_enabled=False,
        _env_file=None,
    )
    database = Database(settings)
    try:
        yield database
    finally:
        await database.dispose()
