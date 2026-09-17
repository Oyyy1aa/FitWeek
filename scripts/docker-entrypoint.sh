#!/bin/sh
set -eu

max_attempts="${DATABASE_WAIT_MAX_ATTEMPTS:-30}"
interval_seconds="${DATABASE_WAIT_INTERVAL_SECONDS:-1}"
attempt=1

while [ "$attempt" -le "$max_attempts" ]; do
    if python - <<'PY'
import asyncio

from app.persistence.database import get_database


async def main() -> int:
    database = get_database()
    try:
        await database.check_connection()
    except Exception:
        return 1
    finally:
        await database.dispose()
    return 0


raise SystemExit(asyncio.run(main()))
PY
    then
        echo "MySQL is ready"
        break
    fi

    echo "MySQL unavailable (attempt ${attempt}/${max_attempts})"
    if [ "$attempt" -eq "$max_attempts" ]; then
        echo "MySQL did not become ready within retry limit" >&2
        exit 1
    fi
    attempt=$((attempt + 1))
    sleep "$interval_seconds"
done

echo "applying database migrations"
alembic upgrade head
echo "starting API"
exec "$@"
