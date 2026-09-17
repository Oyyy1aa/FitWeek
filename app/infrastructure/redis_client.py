"""Minimal, optional Redis connection infrastructure."""

from collections.abc import Awaitable
from functools import lru_cache
from typing import cast

from redis.asyncio import Redis
from redis.exceptions import RedisError

from app.config import Settings, get_settings


class RedisUnavailableError(RuntimeError):
    """Normalized Redis availability failure without connection details."""


class RedisDisabledError(RuntimeError):
    """Raised when a caller requests a disabled Redis client."""


class RedisManager:
    """Own an optional Redis client with bounded operations and safe cleanup."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Redis | None = None

    @property
    def enabled(self) -> bool:
        """Return whether Redis assistance is configured on."""

        return self.settings.redis_enabled

    def _ensure_client(self) -> Redis:
        if not self.enabled:
            raise RedisDisabledError("Redis is disabled")
        if self._client is None:
            self._client = Redis.from_url(
                self.settings.redis_url.get_secret_value(),
                decode_responses=True,
                max_connections=self.settings.redis_max_connections,
                socket_connect_timeout=self.settings.redis_connect_timeout_seconds,
                socket_timeout=self.settings.redis_socket_timeout_seconds,
                health_check_interval=(
                    self.settings.redis_healthcheck_interval_seconds
                ),
            )
        return self._client

    async def start(self) -> None:
        """Verify Redis when enabled without running at module import time."""

        if self.enabled and not await self.ping():
            raise RedisUnavailableError("Redis is unavailable")

    def get_client(self) -> Redis:
        """Return the owned client, creating it without connecting if needed."""

        return self._ensure_client()

    async def ping(self) -> bool:
        """Return a normalized availability result without leaking failures."""

        if not self.enabled:
            return False
        try:
            result = await cast(Awaitable[bool], self._ensure_client().ping())
            return bool(result)
        except (RedisError, OSError, TimeoutError):
            return False

    async def close(self) -> None:
        """Close the client safely; repeated calls are allowed."""

        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def build_key(self, *parts: str) -> str:
        """Build a namespaced key and reject missing or blank segments."""

        if not parts:
            raise ValueError("at least one Redis key segment is required")
        normalized_parts = tuple(part.strip().strip(":") for part in parts)
        if any(not part for part in normalized_parts):
            raise ValueError("Redis key segments must not be blank")
        return ":".join((self.settings.redis_key_prefix, *normalized_parts))


@lru_cache
def get_redis_manager() -> RedisManager:
    """Return the process-level optional Redis manager."""

    return RedisManager()


async def close_redis() -> None:
    """Close the cached Redis manager during application shutdown."""

    await get_redis_manager().close()
