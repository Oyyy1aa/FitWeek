"""Redis implementation of the optional ACTIVE Memory cache boundary."""

import json
import logging
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

from redis.exceptions import RedisError

from app.domain.memory.enums import MemorySource, MemoryStatus, MemoryType
from app.domain.memory.models import UserMemory
from app.infrastructure.redis_client import RedisManager

logger = logging.getLogger(__name__)
_SCHEMA_VERSION = 1


class RedisMemoryCache:
    """Safe JSON cache; every Redis failure is intentionally a cache miss."""

    def __init__(self, redis: RedisManager, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds
        self._bypass_users: set[UUID] = set()

    def _key(self, user_id: UUID) -> str:
        return self._redis.build_key("memory", "active", "v1", str(user_id))

    async def get_active(self, user_id: UUID) -> tuple[UserMemory, ...] | None:
        if user_id in self._bypass_users:
            return None
        try:
            raw = await self._redis.get_client().get(self._key(user_id))
            if raw is None:
                return None
            payload = json.loads(raw)
            if not isinstance(payload, dict) or set(payload) != {
                "schema_version",
                "items",
                "cached_at",
            }:
                raise ValueError("cache payload has an invalid shape")
            if payload["schema_version"] != _SCHEMA_VERSION:
                raise ValueError("cache payload schema is unsupported")
            cached_at = datetime.fromisoformat(str(payload["cached_at"]))
            if cached_at.tzinfo is None or cached_at.utcoffset() != UTC.utcoffset(None):
                raise ValueError("cache cached_at must be UTC")
            values = payload["items"]
            if not isinstance(values, list):
                raise ValueError("cache items are not a list")
            return tuple(
                sorted((self._decode(item) for item in values), key=self._sort_key)
            )
        except (RedisError, OSError, TimeoutError, ValueError, KeyError, TypeError):
            logger.warning("redis_memory_cache_degraded", extra={"operation": "get"})
            await self.invalidate_active(user_id)
            return None

    async def set_active(self, user_id: UUID, memories: tuple[UserMemory, ...]) -> None:
        try:
            payload = json.dumps(
                {
                    "schema_version": _SCHEMA_VERSION,
                    "items": [
                        self._encode(item)
                        for item in sorted(memories, key=self._sort_key)
                    ],
                    "cached_at": datetime.now(UTC).isoformat(),
                },
                sort_keys=True,
            )
            await self._redis.get_client().set(
                self._key(user_id), payload, ex=self._ttl_seconds
            )
            self._bypass_users.discard(user_id)
        except (RedisError, OSError, TimeoutError):
            self._bypass_users.add(user_id)
            logger.warning("redis_memory_cache_degraded", extra={"operation": "set"})
            return

    async def invalidate_active(self, user_id: UUID) -> None:
        try:
            await self._redis.get_client().delete(self._key(user_id))
            self._bypass_users.discard(user_id)
        except (RedisError, OSError, TimeoutError):
            # A failed invalidation must force the following read to MySQL.
            self._bypass_users.add(user_id)
            logger.warning(
                "redis_memory_cache_degraded", extra={"operation": "invalidate"}
            )
            return

    @staticmethod
    def _sort_key(memory: UserMemory) -> tuple[str, str, str]:
        return (memory.memory_type.value, memory.key, str(memory.id))

    @staticmethod
    def _encode(memory: UserMemory) -> dict[str, object]:
        return {
            "id": str(memory.id),
            "user_id": str(memory.user_id),
            "memory_type": memory.memory_type.value,
            "key": memory.key,
            "normalized_value": memory.normalized_value,
            "display_value": memory.display_value,
            "status": memory.status.value,
            "source": memory.source.value,
            "confidence": None if memory.confidence is None else str(memory.confidence),
            "valid_from": memory.valid_from.isoformat(),
            "valid_until": None
            if memory.valid_until is None
            else memory.valid_until.isoformat(),
            "confirmed_at": None
            if memory.confirmed_at is None
            else memory.confirmed_at.isoformat(),
            "created_at": memory.created_at.isoformat(),
            "updated_at": memory.updated_at.isoformat(),
            "deleted_at": None
            if memory.deleted_at is None
            else memory.deleted_at.isoformat(),
            "version": memory.version,
        }

    @staticmethod
    def _decode(value: object) -> UserMemory:
        if not isinstance(value, dict):
            raise ValueError("cache entry is invalid")

        def time(name: str) -> datetime | None:
            raw = value[name]
            return None if raw is None else datetime.fromisoformat(str(raw))

        def required_time(name: str) -> datetime:
            result = time(name)
            if result is None:
                raise ValueError(f"cache {name} is missing")
            return result

        confidence = value["confidence"]
        return UserMemory(
            id=UUID(str(value["id"])),
            user_id=UUID(str(value["user_id"])),
            memory_type=MemoryType(str(value["memory_type"])),
            key=str(value["key"]),
            normalized_value=str(value["normalized_value"]),
            display_value=str(value["display_value"]),
            status=MemoryStatus(str(value["status"])),
            source=MemorySource(str(value["source"])),
            confidence=None if confidence is None else Decimal(str(confidence)),
            valid_from=required_time("valid_from"),
            valid_until=time("valid_until"),
            confirmed_at=time("confirmed_at"),
            created_at=required_time("created_at"),
            updated_at=required_time("updated_at"),
            deleted_at=time("deleted_at"),
            version=int(value["version"]),
        )
