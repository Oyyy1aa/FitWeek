"""Inspect the configured local Redis server without changing any data."""

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from redis.asyncio import Redis  # noqa: E402
from redis.exceptions import RedisError  # noqa: E402

from app.config import Settings  # noqa: E402


async def _key_count(url: str, database: int) -> int:
    client = Redis.from_url(
        url,
        db=database,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
        health_check_interval=10,
    )
    try:
        return int(await client.dbsize())
    finally:
        await client.aclose()


async def main() -> int:
    """Ping Redis and report only non-sensitive server and DB metadata."""

    settings = Settings()
    if not settings.redis_enabled:
        print("Redis check: FAIL (REDIS_ENABLED is false)")
        return 1

    url = settings.redis_url.get_secret_value()
    client = Redis.from_url(
        url,
        decode_responses=True,
        socket_connect_timeout=settings.redis_connect_timeout_seconds,
        socket_timeout=settings.redis_socket_timeout_seconds,
        health_check_interval=settings.redis_healthcheck_interval_seconds,
    )
    try:
        pong = bool(await client.ping())
        version = str((await client.info("server")).get("redis_version", "unknown"))
        db0_keys, db15_keys = await asyncio.gather(
            _key_count(url, 0),
            _key_count(url, 15),
        )
    except (OSError, RedisError, TimeoutError) as exc:
        print(f"Redis check: FAIL ({type(exc).__name__})")
        return 1
    finally:
        await client.aclose()

    print(f"Redis PING: {'PONG' if pong else 'FAIL'}")
    print(f"Redis version: {version}")
    print(f"Redis DB 0 keys: {db0_keys}")
    print(f"Redis DB 15 keys: {db15_keys}")
    return 0 if pong else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
